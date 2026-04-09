"""figmaclaw write-descriptions — update individual frame descriptions in a .md file.

Mechanically replaces description cells in markdown table rows by matching
node_id. No LLM needed — this is a pure find-and-replace operation.

Used for incremental enrichment of large sections where write-body --section
would require the LLM to reproduce hundreds of unchanged rows.

Input: JSON mapping of node_id → description, via --descriptions flag or stdin.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click

from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.git_utils import git_commit

# Match a table row with a backtick-quoted node_id in the second column.
# Captures: (1) everything before the description, (2) the description, (3) trailing |
_ROW_RE = re.compile(
    r"^(\| [^|]+ \| `([^`]+)` \| )"  # prefix: | name | `node_id` |<space>
    r"(.*)"  # description (everything until end)
    r"( \|)\s*$"  # trailing " |"
)


def _update_descriptions(md: str, descriptions: dict[str, str]) -> tuple[str, int]:
    """Replace description cells in table rows matching node_ids.

    Returns (updated_md, count_of_rows_updated).
    """
    lines = md.splitlines()
    updated = 0

    for i, line in enumerate(lines):
        m = _ROW_RE.match(line)
        if m:
            node_id = m.group(2)
            if node_id in descriptions:
                # Escape pipe characters in descriptions to avoid breaking the table
                desc = descriptions[node_id].replace("|", "\\|")
                lines[i] = f"{m.group(1)}{desc} |"
                updated += 1

    return "\n".join(lines), updated


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

    Finds table rows by node_id and replaces the description cell.
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

    updated_text, count = _update_descriptions(md_text, descriptions)
    md_path.write_text(updated_text)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    click.echo(f"write-descriptions: updated {count}/{len(descriptions)} rows in {rel}")

    not_found = set(descriptions.keys()) - {
        m.group(2)
        for line in updated_text.splitlines()
        if (m := _ROW_RE.match(line)) and m.group(2) in descriptions
    }
    if not_found:
        click.echo(
            f"  warning: {len(not_found)} node_id(s) not found in table: {not_found}", err=True
        )

    if auto_commit and git_commit(
        repo_dir, [rel], f"sync: update {count} frame descriptions in {rel}"
    ):
        click.echo(f"  committed: {rel}")
