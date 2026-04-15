"""figmaclaw sync — re-sync a single figmaclaw .md file from the Figma REST API.

Re-fetches the current page structure for one .md file, restores all existing
descriptions from the current file, and updates the YAML frontmatter in-place.
The LLM-authored body (page summary, section intros, Mermaid charts) is NEVER
overwritten — only frontmatter changes.

If the file does not exist yet, writes a new scaffold with LLM placeholders.

This is a pure structure re-sync — it does NOT call an LLM.  To generate or
refresh descriptions, use the figma-enrich-page skill (which calls inspect
to check staleness, then generates descriptions via the agent).

Optional flags:
  --scaffold   Print the scaffold template (with LLM placeholders) to stdout
               without writing it to disk. Useful as a structural hint for the
               LLM when the page changed significantly.
  --show-body  Print the existing body to stdout. The LLM should ALWAYS see
               the existing body so it can preserve prose, adapt descriptions,
               and only change what actually needs changing.

Typical use cases:
- The page structure changed in Figma but you want to re-sync before the next
  scheduled pull.
- Restoring a corrupted or hand-edited .md to the canonical rendered format
  while preserving all existing descriptions.
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import click

from figmaclaw.commands._shared import load_state, require_figma_api_key
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_hash import compute_frame_hashes, compute_page_hash
from figmaclaw.figma_models import from_page_node
from figmaclaw.figma_parse import parse_flows, parse_frontmatter, split_frontmatter
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import PageEntry
from figmaclaw.git_utils import git_commit
from figmaclaw.pull_logic import _merge_existing, update_page_frontmatter, write_new_page


@click.command("sync")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.option(
    "--scaffold",
    "show_scaffold",
    is_flag=True,
    help="Print scaffold template to stdout (does not write).",
)
@click.option("--show-body", "show_body", is_flag=True, help="Print the existing body to stdout.")
@click.pass_context
def sync_cmd(
    ctx: click.Context, md_path: Path, auto_commit: bool, show_scaffold: bool, show_body: bool
) -> None:
    """Re-sync a figmaclaw .md file from the Figma REST API, preserving the body.

    MD_PATH is the path to a figmaclaw-rendered page .md file, e.g.
    figma/web-app/pages/event-landing-page-8421-74664.md

    Fetches the current page structure from Figma, restores all existing
    descriptions, and updates ONLY the frontmatter. The LLM-authored body
    is never overwritten. Does not call an LLM.

    Use --scaffold to print the scaffold template (structural hint for the LLM).
    Use --show-body to print the existing body (so the LLM can preserve it).

    To also generate descriptions, use:  figmaclaw inspect + figma-enrich-page skill
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()

    asyncio.run(_run(api_key, repo_dir, md_path, auto_commit, show_scaffold, show_body))


async def _run(
    api_key: str,
    repo_dir: Path,
    md_path: Path,
    auto_commit: bool,
    show_scaffold: bool = False,
    show_body: bool = False,
) -> None:
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    meta = parse_frontmatter(md_text)
    if meta is None:
        raise click.UsageError(
            f"{md_path}: no figmaclaw frontmatter found — is this a figmaclaw .md file?"
        )

    file_key = meta.file_key
    page_node_id = meta.page_node_id
    md_rel = str(md_path.relative_to(repo_dir))

    click.echo(f"sync: {md_rel}")
    click.echo(f"  file_key={file_key}  page_node_id={page_node_id}")

    state = load_state(repo_dir)

    async with FigmaClient(api_key) as client:
        try:
            file_meta = await client.get_file_meta(file_key)
        except Exception as exc:
            raise click.ClickException(
                f"Failed to fetch file meta for {file_key!r}: {exc}"
            ) from exc

        file_name = file_meta.name
        api_version = file_meta.version
        api_last_modified = file_meta.lastModified

        click.echo("  fetching page from Figma...")
        try:
            page_node = await client.get_page(file_key, page_node_id)
        except Exception as exc:
            raise click.ClickException(f"Failed to fetch page {page_node_id!r}: {exc}") from exc

    new_hash = compute_page_hash(page_node)
    page_slug = md_path.stem

    page = from_page_node(page_node, file_key=file_key, file_name=file_name)
    page = page.model_copy(
        update={"page_slug": page_slug, "version": api_version, "last_modified": api_last_modified}
    )

    existing_flows = parse_flows(md_text)
    page = _merge_existing(page, existing_flows)

    screen_sections = [s for s in page.sections if not s.is_component_library]
    if not screen_sections:
        click.echo("  No screen sections found — nothing to write.")
        return

    screen_page = page.model_copy(update={"sections": screen_sections})
    now = datetime.datetime.now(datetime.UTC).isoformat()

    # Compute per-frame content hashes
    frame_hashes = compute_frame_hashes(page_node)

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
        frame_hashes=frame_hashes,
    )

    # --scaffold: print scaffold template and exit (no write)
    if show_scaffold:
        click.echo("\n--- SCAFFOLD (structural hint for LLM) ---\n")
        click.echo(scaffold_page(screen_page, entry))

    # --show-body: print existing body and exit (no write)
    if show_body:
        parts = split_frontmatter(md_text)
        if parts is not None:
            _, body = parts
            click.echo("\n--- EXISTING BODY (preserve/adapt this) ---\n")
            click.echo(body)

    if show_scaffold or show_body:
        return

    # File exists → update frontmatter only (preserve body)
    # File doesn't exist → write new scaffold
    if md_path.exists():
        update_page_frontmatter(repo_dir, screen_page, entry)
        click.echo(f"  updated frontmatter: {md_rel}")
    else:
        write_new_page(repo_dir, screen_page, entry)
        click.echo(f"  wrote new scaffold: {md_rel}")

    state.set_page_entry(file_key, page_node_id, entry)
    if state.manifest.files.get(file_key):
        state.set_file_meta(
            file_key,
            version=api_version,
            last_modified=api_last_modified,
            last_checked_at=now,
            file_name=file_name,
        )
    state.save()
    click.echo(f"  manifest updated (hash={new_hash})")

    if auto_commit and git_commit(
        repo_dir, [md_rel, ".figma-sync/"], f"sync: re-sync {file_name} / {page_name}"
    ):
        click.echo(f"  committed: {file_name} / {page_name}")
