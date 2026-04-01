"""figmaclaw enrich — re-sync a single figmaclaw .md file from the Figma REST API.

Re-fetches the current page structure for one .md file, restores all existing
descriptions from the current file, and writes the updated .md back in-place.
Updates the manifest so the next pull skips this page (hash is already current).

This is a pure structure re-sync — it does NOT call an LLM.  To generate or
refresh descriptions, use the figma-enrich-page skill (which calls page-tree
then set-frames after generating descriptions via the agent).

Typical use cases:
- The page structure changed in Figma but you want to re-sync before the next
  scheduled pull.
- Restoring a corrupted or hand-edited .md to the canonical rendered format
  while preserving all existing descriptions.
"""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_hash import compute_page_hash
from figmaclaw.figma_models import from_page_node
from figmaclaw.figma_parse import parse_flows, parse_frame_descriptions, parse_page_metadata
from figmaclaw.figma_paths import slugify
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry
from figmaclaw.git_utils import git_commit
from figmaclaw.pull_logic import _merge_existing, write_page


@click.command("enrich")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def enrich_cmd(ctx: click.Context, md_path: Path, auto_commit: bool) -> None:
    """Re-sync a figmaclaw .md file from the Figma REST API, preserving existing descriptions.

    MD_PATH is the path to a figmaclaw-rendered page .md file, e.g.
    figma/web-app/pages/event-landing-page-8421-74664.md

    Fetches the current page structure from Figma, restores all existing
    descriptions, and writes the file back in-place. Does not call an LLM.

    To also generate descriptions, use:  figmaclaw page-tree + figmaclaw set-frames
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.UsageError("FIGMA_API_KEY environment variable is not set.")

    asyncio.run(_run(api_key, repo_dir, md_path, auto_commit))


async def _run(api_key: str, repo_dir: Path, md_path: Path, auto_commit: bool) -> None:
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    meta = parse_page_metadata(md_text)
    if meta is None:
        raise click.UsageError(f"{md_path}: no figmaclaw frontmatter found — is this a figmaclaw .md file?")

    file_key = meta.file_key
    page_node_id = meta.page_node_id
    md_rel = str(md_path.relative_to(repo_dir))

    click.echo(f"enrich: {md_rel}")
    click.echo(f"  file_key={file_key}  page_node_id={page_node_id}")

    state = FigmaSyncState(repo_dir)
    state.load()

    async with FigmaClient(api_key) as client:
        try:
            file_meta = await client.get_file_meta(file_key)
        except Exception as exc:
            raise click.ClickException(f"Failed to fetch file meta for {file_key!r}: {exc}") from exc

        file_name: str = file_meta.get("name", file_key)
        api_version: str = file_meta.get("version", "")
        api_last_modified: str = file_meta.get("lastModified", "")

        click.echo(f"  fetching page from Figma...")
        try:
            page_node = await client.get_page(file_key, page_node_id)
        except Exception as exc:
            raise click.ClickException(f"Failed to fetch page {page_node_id!r}: {exc}") from exc

    new_hash = compute_page_hash(page_node)
    page_slug = md_path.stem

    page = from_page_node(page_node, file_key=file_key, file_name=file_name)
    page = page.model_copy(update={"page_slug": page_slug, "version": api_version, "last_modified": api_last_modified})

    existing_descs = parse_frame_descriptions(md_text)
    existing_flows = parse_flows(md_text)
    page = _merge_existing(page, existing_descs, existing_flows)

    screen_sections = [s for s in page.sections if not s.is_component_library]
    if not screen_sections:
        click.echo("  No screen sections found — nothing to write.")
        return

    screen_page = page.model_copy(update={"sections": screen_sections})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    manifest_file = state.manifest.files.get(file_key)
    existing_page_entry = None
    if manifest_file is not None:
        existing_page_entry = manifest_file.pages.get(page_node_id)

    component_md_paths: list[str] = []
    page_name: str = page_node.get("name", page_slug)
    if existing_page_entry is not None:
        component_md_paths = existing_page_entry.component_md_paths
        page_name = existing_page_entry.page_name

    entry = PageEntry(
        page_name=page_name,
        page_slug=page_slug,
        md_path=md_rel,
        page_hash=new_hash,
        last_refreshed_at=now,
        component_md_paths=component_md_paths,
    )

    write_page(repo_dir, screen_page, entry)
    click.echo(f"  wrote: {md_rel}")

    state.set_page_entry(file_key, page_node_id, entry)
    if state.manifest.files.get(file_key):
        state.set_file_meta(file_key, version=api_version, last_modified=api_last_modified, last_checked_at=now)
    state.save()
    click.echo(f"  manifest updated (hash={new_hash})")

    if auto_commit:
        if git_commit(repo_dir, [md_rel, ".figma-sync/"], f"sync: re-sync {file_name} / {page_name}"):
            click.echo(f"  committed: {file_name} / {page_name}")
