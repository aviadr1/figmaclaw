"""figmaclaw inspect — inspect a figmaclaw .md file without calling the Figma API.

Outputs a compact, agent-friendly view of:
  - file_key and page_node_id (for direct Figma navigation if needed)
  - Each section and its frames
  - Which frames have descriptions and which still need them
  - Summary counts

Use this to check enrichment state before running the enrichment skill.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from figmaclaw.figma_md_parse import parse_sections
from figmaclaw.figma_parse import parse_frontmatter


@click.command("inspect")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--needs-enrichment", "needs_enrichment_only", is_flag=True,
    help="Show only frames/pages that need enrichment.",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")
@click.pass_context
def inspect_cmd(
    ctx: click.Context, md_path: Path, needs_enrichment_only: bool, json_output: bool,
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

    sections = parse_sections(md_text)
    frame_ids = set(meta.frames)  # list of node IDs from frontmatter

    total = sum(len(s.frames) for s in sections)
    # In v2, "missing" means frame ID in body but not in frontmatter (or vice versa)
    # Descriptions live in body, not frontmatter — check body for placeholders
    has_placeholders = "(no description yet)" in md_text or "<!-- LLM:" in md_text
    # Count frames from body that have placeholder descriptions
    missing = 0
    if has_placeholders:
        for line in md_text.splitlines():
            if "| (no description yet) |" in line:
                missing += 1

    if json_output:
        output = {
            "md_path": str(
                md_path.relative_to(repo_dir)
                if md_path.is_relative_to(repo_dir)
                else md_path
            ),
            "file_key": meta.file_key,
            "page_node_id": meta.page_node_id,
            "total_frames": total,
            "missing_descriptions": missing,
            "needs_enrichment": has_placeholders or meta.enriched_hash is None,
            "sections": [
                {
                    "name": s.name,
                    "node_id": s.node_id,
                    "frames": [
                        {
                            "name": f.name,
                            "node_id": f.node_id,
                            "in_frontmatter": f.node_id in frame_ids,
                        }
                        for f in s.frames
                    ],
                }
                for s in sections
            ],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        rel = md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path
        click.echo(f"{rel}")
        click.echo(f"  file_key: {meta.file_key}  page_node_id: {meta.page_node_id}")
        click.echo(f"  {total} frame(s) total, {missing} with placeholder descriptions")
        if meta.enriched_hash:
            click.echo(f"  enriched_hash: {meta.enriched_hash}  enriched_at: {meta.enriched_at}")
        else:
            click.echo("  NOT enriched")
        click.echo("")
        for section in sections:
            click.echo(f"  [{section.name}]  ({section.node_id})")
            for frame in section.frames:
                in_fm = "✓" if frame.node_id in frame_ids else "✗"
                click.echo(f"    {in_fm} {frame.node_id}  {frame.name}")
        click.echo("")

    sys.exit(0)
