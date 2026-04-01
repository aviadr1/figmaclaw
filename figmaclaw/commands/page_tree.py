"""figmaclaw page-tree — inspect a figmaclaw .md file without calling the Figma API.

Outputs a compact, agent-friendly view of:
  - file_key and page_node_id (for direct Figma navigation if needed)
  - Each section and its frames
  - Which frames have descriptions and which still need them
  - Summary counts

Use this before running set-frames to know exactly what descriptions to generate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from figmaclaw.figma_md_parse import parse_sections
from figmaclaw.figma_parse import parse_frontmatter


@click.command("page-tree")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option("--missing-only", is_flag=True, help="Show only frames that need descriptions.")
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")
@click.pass_context
def page_tree_cmd(ctx: click.Context, md_path: Path, missing_only: bool, json_output: bool) -> None:
    """Inspect a figmaclaw page .md — show sections, frames, and description status.

    MD_PATH is the path to a figmaclaw-rendered page .md file. No Figma API call is made.

    Exit code 2 if the file has no figmaclaw frontmatter.
    Exit code 1 if there are frames with missing descriptions (useful for scripting).
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
    frames_dict = meta.frames  # {node_id: description} from frontmatter

    total = sum(len(s.frames) for s in sections)
    missing = sum(1 for s in sections for f in s.frames if not frames_dict.get(f.node_id))

    if json_output:
        output = {
            "md_path": str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path),
            "file_key": meta.file_key,
            "page_node_id": meta.page_node_id,
            "total_frames": total,
            "missing_descriptions": missing,
            "sections": [
                {
                    "name": s.name,
                    "node_id": s.node_id,
                    "frames": [
                        {
                            "name": f.name,
                            "node_id": f.node_id,
                            "description": frames_dict.get(f.node_id) or None,
                            "needs_description": not frames_dict.get(f.node_id),
                        }
                        for f in s.frames
                        if not missing_only or not frames_dict.get(f.node_id)
                    ],
                }
                for s in sections
                if not missing_only or any(not frames_dict.get(f.node_id) for f in s.frames)
            ],
        }
        click.echo(json.dumps(output, indent=2))
    else:
        rel = md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path
        click.echo(f"{rel}")
        click.echo(f"  file_key: {meta.file_key}  page_node_id: {meta.page_node_id}")
        click.echo(f"  {total} frame(s) total, {missing} need description(s)")
        click.echo("")
        for section in sections:
            section_frames = [f for f in section.frames if not missing_only or not frames_dict.get(f.node_id)]
            if not section_frames:
                continue
            click.echo(f"  [{section.name}]  ({section.node_id})")
            for frame in section_frames:
                desc = frames_dict.get(frame.node_id, "")
                status = "✗" if not desc else "✓"
                desc_preview = desc[:60] + "…" if len(desc) > 60 else desc
                desc_str = f"  {desc_preview}" if desc_preview else ""
                click.echo(f"    {status} {frame.node_id}  {frame.name}{desc_str}")
        click.echo("")

    sys.exit(1 if missing else 0)
