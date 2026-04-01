"""figmaclaw set-frames — write agent-generated descriptions into a figmaclaw .md file.

Performs a surgical in-place update:
  1. Merges new descriptions into the `frames:` dict in the YAML frontmatter.
  2. Optionally sets the page summary paragraph.
  3. Optionally replaces the flows list.

No Figma API call is made — the file is updated entirely from the supplied data.
The body table rows are NOT updated here; they are regenerated on the next
`figmaclaw enrich` or `figmaclaw pull`.

Input formats (--frames / --summary / --flows):
  --frames     JSON object: {"node_id": "description", ...}
               Accepts inline JSON or a path to a .json file.
  --summary    Plain text string to set as the page summary paragraph.
  --flows      JSON array: [["src_node_id", "dst_node_id"], ...]

If --frames is not given, reads JSON from stdin.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click

from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import _FlowDict, _FlowList, _FrontmatterDumper
from figmaclaw.git_utils import git_commit

_SUMMARY_RE = re.compile(
    r"(\[Open in Figma\]\([^)]+\)\n\n)"  # anchor line + blank
    r"(?:[^\n#][^\n]*\n\n)?",            # optional existing summary paragraph
    re.MULTILINE,
)


def _apply_frontmatter(md: str, descriptions: dict[str, str], flows: list[list[str]] | None) -> str:
    """Merge descriptions (and optionally flows) into the YAML frontmatter block."""
    import yaml

    fm = parse_frontmatter(md)
    if fm is None:
        return md  # nothing to update

    merged_frames = dict(fm.frames)
    merged_frames.update(descriptions)

    fm_data: dict = {"file_key": fm.file_key, "page_node_id": fm.page_node_id}
    if fm.section_node_id:
        fm_data["section_node_id"] = fm.section_node_id
    if merged_frames:
        fm_data["frames"] = _FlowDict(merged_frames)
    effective_flows = flows if flows is not None else fm.flows
    if effective_flows:
        fm_data["flows"] = _FlowList(effective_flows)

    new_fm_body = yaml.dump(
        fm_data, Dumper=_FrontmatterDumper, default_flow_style=False, allow_unicode=True,
        width=2**20,  # prevent PyYAML from wrapping long flow-style values
    ).rstrip()
    # Replace frontmatter block: md = "---\n{fm}\n---\n{body}" — use partition to avoid regex
    _, sep, after_open = md.partition("---\n")
    if not sep:
        return md
    _, sep2, body = after_open.partition("\n---")
    if not sep2:
        return md
    if body.startswith("\n"):
        body = body[1:]
    return f"---\n{new_fm_body}\n---\n{body}"


def _apply_summary(md: str, summary: str) -> str:
    """Set or replace the page summary paragraph that follows the Figma link."""
    replacement = r"\g<1>" + summary + "\n\n"
    updated, n = _SUMMARY_RE.subn(replacement, md, count=1)
    if n == 0:
        # Anchor line not found (unusual file structure) — leave body unchanged.
        # Descriptions are already in frontmatter; summary can be set on next enrich.
        return md
    return updated


@click.command("set-frames")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--frames",
    "frames_input",
    default=None,
    help='JSON object {"node_id": "description", ...} or path to a .json file. Reads stdin if omitted.',
)
@click.option("--summary", "summary", default=None, help="Page summary paragraph to set.")
@click.option(
    "--flows",
    "flows_input",
    default=None,
    help='JSON array [["src", "dst"], ...] to replace the flows list.',
)
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def set_frames_cmd(
    ctx: click.Context,
    md_path: Path,
    frames_input: str | None,
    summary: str | None,
    flows_input: str | None,
    auto_commit: bool,
) -> None:
    """Write agent-generated descriptions into a figmaclaw .md file.

    MD_PATH is the path to a figmaclaw-rendered page .md file.

    Writes descriptions to the frontmatter `frames:` dict only. Body table rows
    are NOT updated — they are regenerated on the next enrich or pull. Does not
    call the Figma API.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    # Load descriptions
    if frames_input is not None:
        p = Path(frames_input)
        if p.exists():
            raw = p.read_text()
        else:
            raw = frames_input
        try:
            descriptions: dict[str, str] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"--frames: invalid JSON — {exc}") from exc
    else:
        if sys.stdin.isatty():
            raise click.UsageError("Provide --frames JSON or pipe JSON to stdin.")
        raw = sys.stdin.read().strip()
        try:
            descriptions = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"stdin: invalid JSON — {exc}") from exc

    if not isinstance(descriptions, dict):
        raise click.UsageError("--frames must be a JSON object {node_id: description, ...}")

    # Load optional flows
    flows: list[list[str]] | None = None
    if flows_input is not None:
        try:
            flows = json.loads(flows_input)
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"--flows: invalid JSON — {exc}") from exc

    md_text = md_path.read_text()

    # Apply updates
    md_text = _apply_frontmatter(md_text, descriptions, flows)
    if summary is not None:
        md_text = _apply_summary(md_text, summary)

    md_path.write_text(md_text)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    n = len(descriptions)
    click.echo(f"set-frames: wrote {n} description(s) to {rel}")

    if auto_commit:
        if git_commit(repo_dir, [rel], f"sync: set-frames {rel} with {n} description(s)"):
            click.echo(f"  committed: {rel}")
