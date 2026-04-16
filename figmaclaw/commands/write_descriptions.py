"""figmaclaw write-descriptions — update individual frame descriptions in a .md file.

Mechanically replaces description cells in canonical frame/variant markdown table
rows by matching node_id. No LLM needed — this is a pure find-and-replace operation.

Used for incremental enrichment of large sections where write-body --section
would require the LLM to reproduce hundreds of unchanged rows.

Input: JSON mapping of node_id → description, via --descriptions flag or stdin.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from figmaclaw.figma_md_parse import section_line_ranges
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_schema import (
    parse_frame_row,
    render_frame_row,
    render_frame_table_header,
    render_variant_table_header,
)
from figmaclaw.git_utils import git_commit


def _update_descriptions(md: str, descriptions: dict[str, str]) -> tuple[str, int, set[str]]:
    """Replace description cells in canonical frame rows matching node_ids.

    Returns ``(updated_md, updated_count, matched_node_ids)``.
    Only rows inside canonical frame/variant tables within frame sections are editable.
    """
    lines = md.splitlines()
    updated = 0
    matched_node_ids: set[str] = set()

    frame_header, frame_separator = render_frame_table_header()
    variant_header, variant_separator = render_variant_table_header()
    canonical_tables = {
        (frame_header, frame_separator),
        (variant_header, variant_separator),
    }

    for _section, start, end in section_line_ranges(md):
        i = start + 1
        while i < end:
            current = lines[i].strip()
            if i + 1 < end and (current, lines[i + 1].strip()) in canonical_tables:
                i += 2
                while i < end:
                    row_line = lines[i]
                    if not row_line.strip():
                        break
                    row = parse_frame_row(row_line)
                    if row is None:
                        break
                    if row.node_id in descriptions:
                        matched_node_ids.add(row.node_id)
                        # render_frame_row handles pipe escaping and canonical cell formatting.
                        lines[i] = render_frame_row(
                            row.name,
                            row.node_id,
                            descriptions[row.node_id],
                        )
                        updated += 1
                    i += 1
                continue
            i += 1

    return "\n".join(lines), updated, matched_node_ids


@click.command("write-descriptions")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--descriptions",
    "desc_input",
    default=None,
    help='JSON object: {"node_id": "description", ...}. Reads stdin if omitted.',
)
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def write_descriptions_cmd(
    ctx: click.Context,
    md_path: Path,
    desc_input: str | None,
    auto_commit: bool,
) -> None:
    """Update individual frame descriptions in a figmaclaw .md file.

    Finds canonical frame-table rows by node_id and replaces the description cell.
    Does not touch section headings, section intros, page summary,
    or any other content. No Figma API call is made.

    Input is a JSON object mapping node_id to description string.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    if desc_input is not None:
        descriptions = json.loads(desc_input)
    else:
        if sys.stdin.isatty():
            raise click.UsageError("Provide --descriptions or pipe JSON to stdin.")
        descriptions = json.loads(sys.stdin.read())

    if not isinstance(descriptions, dict):
        raise click.UsageError('Descriptions must be a JSON object: {"node_id": "desc", ...}')

    md_text = md_path.read_text()
    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(f"{md_path}: no figmaclaw frontmatter found")

    updated_text, count, matched = _update_descriptions(md_text, descriptions)
    md_path.write_text(updated_text)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    click.echo(f"write-descriptions: updated {count}/{len(descriptions)} rows in {rel}")

    not_found = set(descriptions.keys()) - matched
    if not_found:
        click.echo(
            f"  warning: {len(not_found)} node_id(s) not found in table: {not_found}", err=True
        )

    if auto_commit and git_commit(
        repo_dir, [rel], f"sync: update {count} frame descriptions in {rel}"
    ):
        click.echo(f"  committed: {rel}")
