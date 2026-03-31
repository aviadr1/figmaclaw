"""figmaclaw set-frames — write agent-generated descriptions into a figmaclaw .md file.

Performs a surgical in-place update:
  1. Merges new descriptions into the `frames:` dict in the YAML frontmatter.
  2. Replaces the description cell in every matching table row in the body.
  3. Optionally sets the page summary paragraph.
  4. Optionally replaces the flows list.

No Figma API call is made — the file is updated entirely from the supplied data.

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
import subprocess
import sys
from pathlib import Path

import click

from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import _FlowDict, _FlowList, _FrontmatterDumper

_PLACEHOLDER = "(no description yet)"
_FRONTMATTER_RE = re.compile(r"^(---\n)(.+?\n)(---)", re.DOTALL)
_SUMMARY_RE = re.compile(
    r"(\[Open in Figma\]\([^)]+\)\n\n)"  # anchor line + blank
    r"(?:[^\n#][^\n]*\n\n)?",            # optional existing summary paragraph
    re.MULTILINE,
)


def _build_table_row_re(node_id: str) -> re.Pattern[str]:
    """Match a table row containing `node_id` in the node-id column.

    Targets the LAST column as the description, so it works for both the
    3-column per-section tables and the 4-column Quick Reference table.
    Groups: (prefix including opening '| ' of last col)(description content)(closing ' |')

    Non-greedy .+? combined with the end-of-line anchor ensures the match
    consumes everything up to the last ' |', so descriptions containing
    pipe characters round-trip correctly.
    """
    escaped = re.escape(node_id)
    return re.compile(
        r"^(\| [^|]+ \| `" + escaped + r"` \| )(.+?)( \|)[ \t]*$",
        re.MULTILINE,
    )


def _apply_descriptions(md: str, descriptions: dict[str, str]) -> str:
    """Replace description cells in all table rows for matching node IDs."""
    lines = md.splitlines(keepends=True)
    result: list[str] = []
    for line in lines:
        updated = line
        # Quick scan: only process lines that look like table rows
        if line.startswith("|") and "`" in line:
            for node_id, desc in descriptions.items():
                pattern = _build_table_row_re(node_id)
                m = pattern.search(line)
                if m:
                    updated = pattern.sub(lambda _m, _d=desc: _m.group(1) + _d + _m.group(3), line)
                    break  # each row has at most one node_id
        result.append(updated)
    return "".join(result)


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
        fm_data, Dumper=_FrontmatterDumper, default_flow_style=False, allow_unicode=True
    ).rstrip()
    new_fm_block = f"---\n{new_fm_body}\n---"
    return _FRONTMATTER_RE.sub(new_fm_block, md, count=1)


def _apply_summary(md: str, summary: str) -> str:
    """Set or replace the page summary paragraph that follows the Figma link."""
    replacement = r"\g<1>" + summary + "\n\n"
    updated, n = _SUMMARY_RE.subn(replacement, md, count=1)
    if n == 0:
        # Pattern didn't match — append summary after the Figma link line as fallback
        pass
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

    Surgically updates the frontmatter `frames:` dict and description cells in
    all body tables. Does not call the Figma API or re-render the full file.
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
    md_text = _apply_descriptions(md_text, descriptions)
    if summary is not None:
        md_text = _apply_summary(md_text, summary)

    md_path.write_text(md_text)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    n = len(descriptions)
    click.echo(f"set-frames: wrote {n} description(s) to {rel}")

    if auto_commit:
        subprocess.run(["git", "-C", str(repo_dir), "add", rel], check=False)
        diff = subprocess.run(["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"], check=False)
        if diff.returncode != 0:
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", f"sync: enrich {rel}"],
                check=False,
            )
            click.echo(f"  committed: {rel}")
