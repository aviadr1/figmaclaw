"""figmaclaw mark-enriched — snapshot current hashes as enriched state.

Called after the LLM writes body prose via write-body. Copies the current
page_hash and frame_hashes from the manifest into the frontmatter's
enriched_* fields, marking the page as up-to-date.

This is separate from write-body so that typo fixes (write-body only)
don't falsely mark a page as fully enriched.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import click
import yaml

from figmaclaw.body_validation import validate_markdown_contract
from figmaclaw.figma_frontmatter import CURRENT_ENRICHMENT_SCHEMA_VERSION
from figmaclaw.figma_parse import parse_frontmatter, split_frontmatter
from figmaclaw.figma_render import _FlowDict, _FlowList, _FrontmatterDumper
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.git_utils import git_commit


@click.command("mark-enriched")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def mark_enriched_cmd(ctx: click.Context, md_path: Path, auto_commit: bool) -> None:
    """Mark a page as enriched by snapshotting current hashes into frontmatter.

    Reads the current page_hash and frame_hashes from the manifest, writes
    them into the frontmatter as enriched_hash, enriched_frame_hashes, and
    enriched_at. Body is never touched.

    Call this after write-body to mark the page as up-to-date.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    fm = parse_frontmatter(md_text)
    if fm is None:
        click.echo(f"error: {md_path}: no figmaclaw frontmatter found", err=True)
        ctx.exit(2)
        return

    contract = validate_markdown_contract(md_text, fm.frames)
    if not contract.ok:
        click.echo(
            "error: body/frontmatter contract validation failed before mark-enriched:\n"
            + "\n".join(f"- {m}" for m in contract.messages()),
            err=True,
        )
        ctx.exit(2)
        return

    # Read current hashes from manifest
    state = FigmaSyncState(repo_dir)
    state.load()

    file_entry = state.manifest.files.get(fm.file_key)
    if file_entry is None:
        click.echo(f"error: file_key {fm.file_key!r} not found in manifest", err=True)
        ctx.exit(2)
        return

    page_entry = file_entry.pages.get(fm.page_node_id)
    if page_entry is None:
        click.echo(f"error: page {fm.page_node_id!r} not found in manifest", err=True)
        ctx.exit(2)
        return

    now = datetime.datetime.now(datetime.UTC).isoformat()

    # Build new frontmatter with enrichment state
    parts = split_frontmatter(md_text)
    if parts is None:
        click.echo(f"error: {md_path}: failed to parse frontmatter", err=True)
        ctx.exit(2)
        return
    _, body = parts

    fm_data: dict = {"file_key": fm.file_key, "page_node_id": fm.page_node_id}
    if fm.section_node_id:
        fm_data["section_node_id"] = fm.section_node_id
    if fm.frames:
        fm_data["frames"] = _FlowList(fm.frames)
    if fm.flows:
        fm_data["flows"] = _FlowList(fm.flows)

    # Enrichment state (written/updated by this command)
    fm_data["enriched_hash"] = page_entry.page_hash
    fm_data["enriched_at"] = now
    if page_entry.frame_hashes:
        fm_data["enriched_frame_hashes"] = _FlowDict(page_entry.frame_hashes)
    fm_data["enriched_schema_version"] = CURRENT_ENRICHMENT_SCHEMA_VERSION

    # Preserve pull-pass fields — these are written by the pull pass and must not
    # be dropped when mark-enriched rewrites frontmatter. Dropping them was a bug.
    if fm.component_set_keys:
        fm_data["component_set_keys"] = _FlowDict(fm.component_set_keys)
    if fm.raw_frames:
        fm_data["raw_frames"] = _FlowDict(
            {k: _FlowDict({"raw": v.raw, "ds": _FlowList(v.ds)}) for k, v in fm.raw_frames.items()}
        )

    new_fm_body = yaml.dump(
        fm_data,
        Dumper=_FrontmatterDumper,
        default_flow_style=False,
        allow_unicode=True,
        width=2**20,
    ).rstrip()

    md_path.write_text(f"---\n{new_fm_body}\n---\n{body}")

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    click.echo(f"mark-enriched: {rel} (hash={page_entry.page_hash})")

    if auto_commit and git_commit(repo_dir, [rel], f"sync: mark {rel} as enriched"):
        click.echo(f"  committed: {rel}")
