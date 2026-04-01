"""figmaclaw replace-body — replace the body of a figmaclaw .md file, preserving frontmatter.

This is the inverse of set-frames: set-frames writes frontmatter only,
replace-body writes body only. Together they let the LLM update any part
of a .md file without ever touching the other part.

The LLM uses this after rewriting body prose (page summary, section intros,
description tables, Mermaid charts). The frontmatter is never modified.

Input: the new body content, via stdin or --body flag.

Body preservation invariant: frontmatter is byte-for-byte preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from figmaclaw.figma_parse import parse_frontmatter, split_frontmatter
from figmaclaw.git_utils import git_commit


def _replace_body(md: str, new_body: str) -> str:
    """Replace the body of a figmaclaw .md file, preserving frontmatter byte-for-byte.

    Uses split_frontmatter() to separate frontmatter from body, then
    reconstructs the file with the original frontmatter and new body.
    """
    parts = split_frontmatter(md)
    if parts is None:
        return md
    fm_body, _ = parts

    new_body = new_body.strip("\n")
    return f"---\n{fm_body}\n---\n\n{new_body}\n"


@click.command("replace-body")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--body",
    "body_input",
    default=None,
    help="New body content (string or path to a file). Reads stdin if omitted.",
)
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def replace_body_cmd(
    ctx: click.Context,
    md_path: Path,
    body_input: str | None,
    auto_commit: bool,
) -> None:
    """Replace the body of a figmaclaw .md file, preserving frontmatter.

    MD_PATH is the path to a figmaclaw-rendered page .md file.

    Writes new body content below the frontmatter. The YAML frontmatter
    block is never modified. Does not call the Figma API.

    Use this after the LLM has rewritten the body prose.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    # Load new body
    if body_input is not None:
        p = Path(body_input)
        if p.exists():
            new_body = p.read_text()
        else:
            new_body = body_input
    else:
        if sys.stdin.isatty():
            raise click.UsageError("Provide --body or pipe body content to stdin.")
        new_body = sys.stdin.read()

    md_text = md_path.read_text()

    # Validate this is a figmaclaw file
    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(f"{md_path}: no figmaclaw frontmatter found — is this a figmaclaw .md file?")

    updated = _replace_body(md_text, new_body)
    md_path.write_text(updated)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    click.echo(f"replace-body: updated body in {rel}")

    if auto_commit:
        if git_commit(repo_dir, [rel], f"sync: update body prose in {rel}"):
            click.echo(f"  committed: {rel}")
