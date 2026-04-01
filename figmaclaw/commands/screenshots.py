"""figmaclaw screenshots — download frame screenshots to local cache.

Fetches screenshots for all frames in a figmaclaw .md file via the Figma
image export REST API and saves them to .figma-cache/screenshots/{file_key}/.

Outputs a JSON manifest so the calling agent knows which local files to read.

Use case: CI/CD environments where the Figma MCP plugin is unavailable.
The agent reads the local PNG files with the Read tool instead of calling
get_screenshot via MCP.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
from typing import Any

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_md_parse import parse_sections
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_paths import screenshot_cache_path

_FIGMA_IMAGE_BATCH = 50
_MAX_CONCURRENT_DOWNLOADS = 10
_DOWNLOAD_LOCK_FILENAME = ".figma-downloads.lock"


@click.command("screenshots")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--pending", "pending_only", is_flag=True, default=False,
    help="Only download frames that have no description yet.",
)
@click.pass_context
def screenshots_cmd(ctx: click.Context, md_path: Path, pending_only: bool) -> None:
    """Download frame screenshots for a figmaclaw .md file to local cache.

    Saves PNGs to .figma-cache/screenshots/{file_key}/ (gitignored).
    Outputs a JSON manifest: {file_key, screenshots: [{node_id, path}]}.

    The agent can then read local PNG files with the Read tool instead of
    calling get_screenshot via Figma MCP — enabling enrichment in CI where
    MCP plugins are unavailable.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.UsageError("FIGMA_API_KEY environment variable is not set.")

    result = asyncio.run(_run(api_key, repo_dir, md_path, pending_only))
    click.echo(json.dumps(result, indent=2))


async def _run(api_key: str, repo_dir: Path, md_path: Path, pending_only: bool) -> dict:
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(f"{md_path}: no figmaclaw frontmatter — is this a figmaclaw .md file?")

    file_key = fm.file_key

    # Node IDs come from the body (parse_sections) — covers pages where fm.frames
    # is empty because no descriptions have been written yet.
    all_body_ids = [f.node_id for s in parse_sections(md_text) for f in s.frames]

    if pending_only:
        # Pending = in the body but not yet described in frontmatter.
        node_ids = [nid for nid in all_body_ids if not fm.frames.get(nid)]
    else:
        node_ids = all_body_ids

    if not node_ids:
        return {"file_key": file_key, "screenshots": []}

    lock_path = repo_dir / ".figma-cache" / _DOWNLOAD_LOCK_FILENAME

    def _acquire() -> Any:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def _release(f: Any) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()

    lock_fd = await asyncio.to_thread(_acquire)
    try:
        async with FigmaClient(api_key) as client:
            all_urls: dict[str, str | None] = {}
            for i in range(0, len(node_ids), _FIGMA_IMAGE_BATCH):
                batch = node_ids[i : i + _FIGMA_IMAGE_BATCH]
                urls = await client.get_image_urls(file_key, batch)
                all_urls.update(urls)

            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)
            tasks = [
                _download_one(client, semaphore, repo_dir, file_key, node_id, url)
                for node_id, url in all_urls.items()
                if url is not None
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await asyncio.to_thread(_release, lock_fd)

    screenshots = [r for r in results if isinstance(r, dict)]
    return {"file_key": file_key, "screenshots": screenshots}


async def _download_one(
    client: FigmaClient,
    semaphore: asyncio.Semaphore,
    repo_dir: Path,
    file_key: str,
    node_id: str,
    url: str,
) -> dict | None:
    async with semaphore:
        try:
            data = await client.download_url(url)
        except Exception:
            return None

    out_path = screenshot_cache_path(repo_dir, file_key, node_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)

    try:
        rel = str(out_path.relative_to(repo_dir))
    except ValueError:
        rel = str(out_path)

    return {"node_id": node_id, "path": rel}
