"""figmaclaw write-body — write body prose into a figmaclaw .md file.

The LLM uses this after generating page descriptions from screenshots.
Writes body content (page summary, section intros, description tables,
Mermaid charts) while preserving frontmatter byte-for-byte.

Supports two modes:
  - Full body replacement (default): writes the entire body.
  - Section replacement (--section): surgically replaces one section,
    preserving page summary, other sections, and Screen flows.

Input: the new body/section content, via stdin or --body flag.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from figmaclaw.figma_md_parse import _ANY_H2_RE, _SECTION_RE, section_line_ranges
from figmaclaw.figma_parse import parse_frontmatter, split_frontmatter
from figmaclaw.git_utils import git_commit


def _write_body(md: str, new_body: str) -> str:
    """Replace the body of a figmaclaw .md file, preserving frontmatter byte-for-byte."""
    parts = split_frontmatter(md)
    if parts is None:
        return md
    fm_body, _ = parts

    new_body = new_body.strip("\n")
    return f"---\n{fm_body}\n---\n\n{new_body}\n"


def _write_section(md: str, section_node_id: str, new_section_text: str) -> str:
    """Replace one section in the body, preserving everything else.

    Finds the section by matching ``## Name (`section_node_id`)`` in the body.
    Replaces from that heading up to (but not including) the next ``## `` heading
    or end of file. Frontmatter, page summary, other sections, and Screen flows
    are preserved byte-for-byte.

    *new_section_text* must include the ``## `` heading, section intro, and table.
    """
    parts = split_frontmatter(md)
    if parts is None:
        raise ValueError("No frontmatter found")
    fm_body, body = parts

    lines = body.split("\n")
    section_start: int | None = None
    section_end: int | None = None

    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m and m.group(2) == section_node_id:
            section_start = i
            continue
        # After finding our section, the next ## heading marks the end
        if section_start is not None and section_end is None and _ANY_H2_RE.match(line):
            section_end = i
            break

    if section_start is None:
        raise ValueError(f"Section `{section_node_id}` not found in body")

    if section_end is None:
        section_end = len(lines)

    # Strip trailing blank lines from section_end region to avoid accumulation
    new_section_lines = new_section_text.rstrip("\n").split("\n")
    new_lines = lines[:section_start] + new_section_lines + [""] + lines[section_end:]
    new_body = "\n".join(new_lines)

    return f"---\n{fm_body}\n---\n{new_body}"


@click.command("write-body")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--body",
    "body_input",
    default=None,
    help="New body content (string or path to a file). Reads stdin if omitted.",
)
@click.option(
    "--section",
    "section_node_id",
    default=None,
    help="Replace only this section (by node_id) instead of the full body.",
)
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def write_body_cmd(
    ctx: click.Context,
    md_path: Path,
    body_input: str | None,
    section_node_id: str | None,
    auto_commit: bool,
) -> None:
    """Write body prose into a figmaclaw .md file, preserving frontmatter.

    MD_PATH is the path to a figmaclaw-rendered page .md file.

    Default mode: writes new body content below the frontmatter (full replace).
    With --section: surgically replaces one section, preserving everything else.

    Does not call the Figma API.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    if body_input is not None:
        p = Path(body_input)
        if p.exists():
            new_content = p.read_text()
        else:
            new_content = body_input
    else:
        if sys.stdin.isatty():
            raise click.UsageError("Provide --body or pipe body content to stdin.")
        new_content = sys.stdin.read()

    md_text = md_path.read_text()

    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(f"{md_path}: no figmaclaw frontmatter found — is this a figmaclaw .md file?")

    if section_node_id:
        try:
            updated = _write_section(md_text, section_node_id, new_content)
        except ValueError as e:
            raise click.UsageError(str(e))
    else:
        updated = _write_body(md_text, new_content)

    md_path.write_text(updated)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    mode = f"section {section_node_id}" if section_node_id else "body"
    click.echo(f"write-body: updated {mode} in {rel}")

    if auto_commit:
        msg = f"sync: write {mode} in {rel}"
        if git_commit(repo_dir, [rel], msg):
            click.echo(f"  committed: {rel}")
