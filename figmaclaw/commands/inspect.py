"""figmaclaw inspect — inspect a figmaclaw .md file without calling the Figma API.

Outputs a compact, agent-friendly view of:
  - file_key and page_node_id (for direct Figma navigation if needed)
  - Each section and its frames
  - Which frames have descriptions and which still need them
  - Per-section pending/stale counts for section-level enrichment
  - Summary counts

Use this to check enrichment state before running the enrichment skill.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from figmaclaw.commands._shared import load_state
from figmaclaw.figma_frontmatter import (
    CURRENT_ENRICHMENT_SCHEMA_VERSION,
    CURRENT_PULL_SCHEMA_VERSION,
    MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION,
)
from figmaclaw.figma_md_parse import section_line_ranges
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_schema import is_unresolved_row
from figmaclaw.schema_status import enrichment_schema_status, is_pull_schema_stale
from figmaclaw.staleness import stale_frame_ids_from_manifest

SECTION_THRESHOLD = 80


def _count_pending_in_range(lines: list[str], start: int, end: int) -> int:
    """Count placeholder frame rows within a line range.

    Uses :func:`figma_schema.is_unresolved_row` as the canonical check so
    this and the enrichment dispatcher agree on what "pending" means.
    """
    return sum(1 for line in lines[start:end] if is_unresolved_row(line))


def _stale_frame_ids(
    repo_dir: Path,
    file_key: str,
    page_node_id: str,
    enriched_frame_hashes: dict[str, str] | None,
) -> set[str]:
    """Return frame IDs whose manifest hash differs from enriched hash.

    Reads the manifest (cache) to get current frame hashes, compares against
    enriched_frame_hashes from frontmatter (state). Per D4 policy: manifest is
    cache (recomputable), frontmatter is state (persistent).
    """
    try:
        from figmaclaw.figma_sync_state import FigmaSyncState

        state = FigmaSyncState(repo_dir)
        state.load()
    except Exception:
        return set()  # no manifest — can't determine staleness

    stale = stale_frame_ids_from_manifest(
        state,
        file_key=file_key,
        page_node_id=page_node_id,
        enriched_frame_hashes=enriched_frame_hashes,
    )
    return stale or set()


@click.command("inspect")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--needs-enrichment",
    "needs_enrichment_only",
    is_flag=True,
    help="Show only frames/pages that need enrichment.",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")
@click.pass_context
def inspect_cmd(
    ctx: click.Context,
    md_path: Path,
    needs_enrichment_only: bool,
    json_output: bool,
) -> None:
    """Inspect a figmaclaw page .md — show sections, frames, and enrichment status.

    MD_PATH is the path to a figmaclaw-rendered page .md file. No Figma API call is made.

    Exit code 2 if the file has no figmaclaw frontmatter.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    meta = parse_frontmatter(md_text)
    if meta is None:
        click.echo(f"error: {md_path}: no figmaclaw frontmatter found", err=True)
        sys.exit(2)

    ranges = section_line_ranges(md_text)
    lines = md_text.splitlines()
    frame_ids = set(meta.frames)

    # Compute stale frames from manifest vs enriched hashes
    stale_ids = _stale_frame_ids(
        repo_dir,
        meta.file_key,
        meta.page_node_id,
        meta.enriched_frame_hashes,
    )

    total = 0
    missing = 0
    total_sections = 0
    pending_section_count = 0
    stale_section_count = 0
    section_data = []

    for section, start, end in ranges:
        if not section.node_id:
            continue  # skip Screen flows etc.
        total_sections += 1
        section_frames = len(section.frames)
        total += section_frames
        section_pending = _count_pending_in_range(lines, start, end)
        missing += section_pending
        section_stale = sum(1 for f in section.frames if f.node_id in stale_ids)
        if section_pending > 0:
            pending_section_count += 1
        if section_stale > 0:
            stale_section_count += 1

        section_data.append(
            {
                "name": section.name,
                "node_id": section.node_id,
                "total_frames": section_frames,
                "pending_frames": section_pending,
                "stale_frames": section_stale,
                "frames": [
                    {
                        "name": f.name,
                        "node_id": f.node_id,
                        "in_frontmatter": f.node_id in frame_ids,
                    }
                    for f in section.frames
                ],
            }
        )

    has_placeholders = missing > 0 or "<!-- LLM:" in md_text

    # Schema staleness: pull-pass frontmatter fields
    try:
        file_entry = load_state(repo_dir).manifest.files.get(meta.file_key)
        file_pull_schema_version = file_entry.pull_schema_version if file_entry else 0
    except Exception:
        file_pull_schema_version = 0
    pull_schema_stale = is_pull_schema_stale(file_pull_schema_version)

    # Schema staleness: enrichment prompt/format
    enrichment = enrichment_schema_status(meta.enriched_schema_version)
    esv = enrichment.version  # 0 if pre-versioning or never enriched
    enrichment_must_update = enrichment.must_update
    enrichment_should_update = enrichment.should_update

    needs_enrichment = (
        has_placeholders
        or meta.enriched_hash is None
        or stale_section_count > 0
        or enrichment_must_update
    )

    if json_output:
        output = {
            "md_path": str(
                md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path
            ),
            "file_key": meta.file_key,
            "page_node_id": meta.page_node_id,
            "total_frames": total,
            "missing_descriptions": missing,
            "needs_enrichment": needs_enrichment,
            "total_sections": total_sections,
            "pending_sections": pending_section_count,
            "stale_sections": stale_section_count,
            "section_threshold": SECTION_THRESHOLD,
            "pull_schema_stale": pull_schema_stale,
            "pull_schema_version": file_pull_schema_version,
            "current_pull_schema_version": CURRENT_PULL_SCHEMA_VERSION,
            "enrichment_schema_version": esv,
            "enrichment_must_update": enrichment_must_update,
            "enrichment_should_update": enrichment_should_update,
            "current_enrichment_schema_version": CURRENT_ENRICHMENT_SCHEMA_VERSION,
            "min_required_enrichment_schema_version": MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION,
            "sections": section_data,
        }
        click.echo(json.dumps(output, indent=2))
    else:
        rel = md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path
        click.echo(f"{rel}")
        click.echo(f"  file_key: {meta.file_key}  page_node_id: {meta.page_node_id}")
        click.echo(f"  {total} frame(s) total, {missing} pending, {len(stale_ids)} stale")
        click.echo(
            f"  {total_sections} sections ({pending_section_count} pending, {stale_section_count} stale)"
        )
        if meta.enriched_hash:
            click.echo(f"  enriched_hash: {meta.enriched_hash}  enriched_at: {meta.enriched_at}")
        else:
            click.echo("  NOT enriched")
        if pull_schema_stale:
            click.echo(
                f"  [PULL-SCHEMA STALE] frontmatter v{file_pull_schema_version} < current v{CURRENT_PULL_SCHEMA_VERSION} — pull-only refresh needed"
            )
        if enrichment_must_update:
            click.echo(
                f"  [ENRICH MUST] enrichment v{esv} < required v{MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION} — body must be re-enriched"
            )
        elif enrichment_should_update:
            click.echo(
                f"  [ENRICH SHOULD] enrichment v{esv} < current v{CURRENT_ENRICHMENT_SCHEMA_VERSION} — body should be re-enriched (opportunistic)"
            )
        click.echo("")
        for sd in section_data:
            status = ""
            if sd["pending_frames"] > 0:
                status += f" [{sd['pending_frames']} pending]"
            if sd["stale_frames"] > 0:
                status += f" [{sd['stale_frames']} stale]"
            click.echo(f"  [{sd['name']}]  ({sd['node_id']})  {sd['total_frames']} frames{status}")
            for frame in sd["frames"]:
                in_fm = "✓" if frame["in_frontmatter"] else "✗"
                click.echo(f"    {in_fm} {frame['node_id']}  {frame['name']}")
        click.echo("")

    sys.exit(0)
