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
from pathlib import Path
from typing import Any

import click

from figmaclaw.commands._shared import load_state, require_figma_api_key
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_md_parse import parse_sections
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_paths import screenshot_cache_path
from figmaclaw.image_export import DEFAULT_IMAGE_BATCH_SIZE, get_image_urls_batched
from figmaclaw.staleness import stale_frame_ids_from_manifest

_MAX_CONCURRENT_DOWNLOADS = 10
_DOWNLOAD_LOCK_FILENAME = ".figma-downloads.lock"
_ALLOWED_SCREENSHOT_EXTS = {".png", ".jpg", ".jpeg", ".svg"}


def _is_valid_cached_screenshot(path: Path) -> bool:
    """Return True when a cached screenshot path is a supported non-empty file."""
    return (
        path.is_file()
        and path.suffix.lower() in _ALLOWED_SCREENSHOT_EXTS
        and path.stat().st_size > 0
    )


@click.command("screenshots")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--pending",
    "pending_only",
    is_flag=True,
    default=False,
    help="Only download frames that have no description yet.",
)
@click.option(
    "--stale",
    "stale_only",
    is_flag=True,
    default=False,
    help="Only download frames whose content hash changed since last enrichment.",
)
@click.option(
    "--section",
    "section_node_id",
    default=None,
    help="Only download frames belonging to this section (by node_id).",
)
@click.pass_context
def screenshots_cmd(
    ctx: click.Context,
    md_path: Path,
    pending_only: bool,
    stale_only: bool,
    section_node_id: str | None,
) -> None:
    """Download frame screenshots for a figmaclaw .md file to local cache.

    Saves PNGs to .figma-cache/screenshots/{file_key}/ (gitignored).
    Outputs a JSON manifest: {file_key, screenshots: [{node_id, path}]}.

    The agent can then read local PNG files with the Read tool instead of
    calling get_screenshot via Figma MCP — enabling enrichment in CI where
    MCP plugins are unavailable.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()

    result = asyncio.run(
        _run(api_key, repo_dir, md_path, pending_only, stale_only, section_node_id)
    )
    click.echo(json.dumps(result, indent=2))


async def _run(
    api_key: str,
    repo_dir: Path,
    md_path: Path,
    pending_only: bool,
    stale_only: bool,
    section_node_id: str | None = None,
) -> dict:
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(
            f"{md_path}: no figmaclaw frontmatter — is this a figmaclaw .md file?"
        )

    file_key = fm.file_key
    sections = parse_sections(md_text)

    # Node IDs come from the body (parse_sections) — covers pages where fm.frames
    # is empty because no descriptions have been written yet.
    all_body_ids = [f.node_id for s in sections for f in s.frames]

    # Section filter: restrict to frames in one section
    if section_node_id:
        section_frames: set[str] = set()
        for s in sections:
            if s.node_id == section_node_id:
                section_frames = {f.node_id for f in s.frames}
                break
        all_body_ids = [nid for nid in all_body_ids if nid in section_frames]

    if stale_only:
        # Stale = frames whose content hash changed since last enrichment.
        # Compare manifest frame_hashes (current) vs frontmatter
        # enriched_frame_hashes (at last enrichment).
        state = load_state(repo_dir)
        stale_ids = stale_frame_ids_from_manifest(
            state,
            file_key=file_key,
            page_node_id=fm.page_node_id,
            enriched_frame_hashes=fm.enriched_frame_hashes,
        )
        if stale_ids is None:
            # No manifest entry — all frames are stale
            stale_ids = set(all_body_ids)
        node_ids = [nid for nid in all_body_ids if nid in stale_ids]
    elif pending_only:
        # Pending = frames whose body table row has the placeholder
        # description. Both the placeholder check and the node_id
        # extraction come from figma_schema so this can't drift from the
        # enrichment dispatcher's notion of "pending".
        from figmaclaw.figma_schema import is_placeholder_row, parse_frame_row

        pending_ids: set[str] = set()
        for line in md_text.splitlines():
            if not is_placeholder_row(line):
                continue
            row = parse_frame_row(line)
            if row is not None:
                pending_ids.add(row.node_id)
        node_ids = [nid for nid in all_body_ids if nid in pending_ids]
    else:
        node_ids = all_body_ids

    if not node_ids:
        return {"file_key": file_key, "screenshots": []}

    requested_node_ids = list(node_ids)

    # In non-stale modes, reuse existing local cache files to avoid repeated
    # downloads during local retries. In --stale mode we always refresh.
    cached_screenshots: list[dict[str, str]] = []
    if not stale_only:
        uncached_ids: list[str] = []
        invalid_cache_count = 0
        for node_id in node_ids:
            cached_path = screenshot_cache_path(repo_dir, file_key, node_id)
            if _is_valid_cached_screenshot(cached_path):
                try:
                    rel = str(cached_path.relative_to(repo_dir))
                except ValueError:
                    rel = str(cached_path)
                cached_screenshots.append({"node_id": node_id, "path": rel})
            else:
                if cached_path.exists():
                    invalid_cache_count += 1
                uncached_ids.append(node_id)
        click.echo(
            (
                f"[screenshots] cache hits={len(cached_screenshots)} "
                f"misses={len(uncached_ids)} invalid={invalid_cache_count}"
            ),
            err=True,
        )
        node_ids = uncached_ids

    if not node_ids:
        click.echo(
            f"[screenshots] cache satisfied all {len(requested_node_ids)} frame(s); no fetch needed",
            err=True,
        )
        return {"file_key": file_key, "screenshots": cached_screenshots, "failed": []}

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
            all_urls = await get_image_urls_batched(
                client,
                file_key,
                node_ids,
                batch_size=DEFAULT_IMAGE_BATCH_SIZE,
                fill_none_on_batch_error=True,
            )

            semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)
            tasks = [
                _download_one(client, semaphore, repo_dir, file_key, node_id, url)
                for node_id, url in all_urls.items()
                if url is not None
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await asyncio.to_thread(_release, lock_fd)

    downloaded = [r for r in results if isinstance(r, dict)]
    screenshot_by_id = {s["node_id"]: s for s in cached_screenshots}
    screenshot_by_id.update({s["node_id"]: s for s in downloaded})
    screenshots = [screenshot_by_id[nid] for nid in requested_node_ids if nid in screenshot_by_id]
    # Frames where Figma returned null URL (hidden/deleted/unrenderable)
    null_url = {nid for nid, url in all_urls.items() if url is None}
    # Frames where download failed (URL existed but PNG download errored)
    downloaded_ids = {r["node_id"] for r in screenshots}
    download_failed = {
        nid for nid, url in all_urls.items() if url is not None and nid not in downloaded_ids
    }
    failed = sorted(null_url | download_failed)
    return {"file_key": file_key, "screenshots": screenshots, "failed": failed}


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
