"""figmaclaw write-body — write body prose into a figmaclaw .md file.

The LLM uses this after generating page descriptions from screenshots.
Writes body content (page summary, section intros, description tables,
Mermaid charts) while preserving frontmatter byte-for-byte.

Input: the new body content, via stdin or --body flag.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

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


@click.command("write-body")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--body",
    "body_input",
    default=None,
    help="New body content (string or path to a file). Reads stdin if omitted.",
)
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def write_body_cmd(
    ctx: click.Context,
    md_path: Path,
    body_input: str | None,
    auto_commit: bool,
) -> None:
    """Write body prose into a figmaclaw .md file, preserving frontmatter.

    MD_PATH is the path to a figmaclaw-rendered page .md file.

    Writes new body content below the frontmatter. The YAML frontmatter
    block is never modified. Does not call the Figma API.

    Use this after the LLM has written page descriptions from screenshots.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

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

    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(f"{md_path}: no figmaclaw frontmatter found — is this a figmaclaw .md file?")

    updated = _write_body(md_text, new_body)
    md_path.write_text(updated)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    click.echo(f"write-body: updated body in {rel}")

    if auto_commit:
        if git_commit(repo_dir, [rel], f"sync: write body prose in {rel}"):
            click.echo(f"  committed: {rel}")
