"""figmaclaw diff — show what designers actually changed in Figma design files.

Analyzes git history of figma/ markdown files to surface structural design
changes (new pages, added/removed frames, flow changes) while filtering out
enrichment-only changes (body prose, enriched_hash, enriched_at, etc.).

The key insight: frontmatter fields ``frames:`` and ``flows:`` only change
when ``figmaclaw pull`` syncs actual Figma design changes. Body descriptions
and enrichment hashes change during enrichment and are not design changes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from figmaclaw.figma_md_parse import ParsedFrame, section_line_ranges
from figmaclaw.figma_parse import parse_frontmatter

# ── Duration parsing ────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)

_DURATION_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def _parse_duration(since: str) -> timedelta:
    """Convert ``7d``, ``2w``, ``1m``, ``1y`` to a timedelta."""
    m = _DURATION_RE.match(since.strip())
    if not m:
        raise click.BadParameter(
            f"Cannot parse duration {since!r}. Use e.g. '7d', '2w', '1m', '1y'.",
            param_hint="--since",
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(days=_DURATION_DAYS[unit] * n)


def _duration_to_git_since(since: str) -> str:
    """Convert ``7d`` → ``7.days.ago``, ``2w`` → ``14.days.ago``, etc."""
    delta = _parse_duration(since)
    return f"{delta.days}.days.ago"


# ── Git helpers ─────────────────────────────────────────────────────


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result."""
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, check=False,
    )


def _changed_files(repo_dir: Path, git_since: str, target: str) -> list[str]:
    """Return .md files under *target* that changed in git since *git_since*."""
    result = _git(
        repo_dir, "log", f"--since={git_since}", "--diff-filter=AMDRC",
        "--name-only", "--pretty=format:", "--", target,
    )
    if result.returncode != 0:
        return []
    seen: set[str] = set()
    paths: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and line.endswith(".md") and line not in seen:
            seen.add(line)
            paths.append(line)
    return paths


def _file_at_ref(repo_dir: Path, ref: str, path: str) -> str | None:
    """Return file contents at a git ref, or None if it doesn't exist."""
    result = _git(repo_dir, "show", f"{ref}:{path}")
    if result.returncode != 0:
        return None
    return result.stdout


def _oldest_commit_since(repo_dir: Path, git_since: str, path: str) -> str | None:
    """Return the oldest commit hash that touched *path* since *git_since*."""
    result = _git(
        repo_dir, "log", f"--since={git_since}", "--format=%H",
        "--reverse", "--", path,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().splitlines()[0]


# ── Frame name lookup ───────────────────────────────────────────────


def _frame_names_from_body(md: str) -> dict[str, str]:
    """Extract {node_id: frame_name} from body tables."""
    names: dict[str, str] = {}
    for section, _, _ in section_line_ranges(md):
        for frame in section.frames:
            names[frame.node_id] = frame.name
    return names


# ── Diff data structures ────────────────────────────────────────────


@dataclass
class FrameChange:
    node_id: str
    name: str = ""


@dataclass
class FrameRename:
    node_id: str
    old_name: str
    new_name: str


@dataclass
class PageDiff:
    path: str
    file_key: str = ""
    page_node_id: str = ""
    is_new: bool = False
    total_frames: int = 0
    added_frames: list[FrameChange] = field(default_factory=list)
    removed_frames: list[FrameChange] = field(default_factory=list)
    renamed_frames: list[FrameRename] = field(default_factory=list)
    added_flows: list[list[str]] = field(default_factory=list)
    removed_flows: list[list[str]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return (
            self.is_new
            or bool(self.added_frames)
            or bool(self.removed_frames)
            or bool(self.renamed_frames)
            or bool(self.added_flows)
            or bool(self.removed_flows)
        )


# ── Core diff logic ─────────────────────────────────────────────────


def compute_diff(
    repo_dir: Path, target: str, since: str,
) -> tuple[list[PageDiff], datetime, datetime]:
    """Compute design-only diffs for all changed files.

    Returns (diffs, since_date, until_date).
    """
    git_since = _duration_to_git_since(since)
    delta = _parse_duration(since)
    now = datetime.now(timezone.utc)
    since_date = now - delta
    until_date = now

    changed = _changed_files(repo_dir, git_since, target)
    diffs: list[PageDiff] = []

    for rel_path in changed:
        abs_path = repo_dir / rel_path
        if not abs_path.exists():
            continue  # file was deleted

        current_md = abs_path.read_text()
        current_fm = parse_frontmatter(current_md)
        if current_fm is None:
            continue  # not a figmaclaw file

        # Get old version
        oldest = _oldest_commit_since(repo_dir, git_since, rel_path)
        if oldest is None:
            continue

        # Get file at the commit before the oldest change
        old_md = _file_at_ref(repo_dir, f"{oldest}~1", rel_path)

        diff = PageDiff(
            path=rel_path,
            file_key=current_fm.file_key,
            page_node_id=current_fm.page_node_id,
            total_frames=len(current_fm.frames),
        )

        if old_md is None:
            # New file — no old version exists
            diff.is_new = True
            current_names = _frame_names_from_body(current_md)
            for nid in current_fm.frames:
                diff.added_frames.append(FrameChange(
                    node_id=nid, name=current_names.get(nid, ""),
                ))
            diff.added_flows = [list(edge) for edge in current_fm.flows]
        else:
            old_fm = parse_frontmatter(old_md)
            if old_fm is None:
                # File existed but wasn't a figmaclaw file before — treat as new
                diff.is_new = True
                current_names = _frame_names_from_body(current_md)
                for nid in current_fm.frames:
                    diff.added_frames.append(FrameChange(
                        node_id=nid, name=current_names.get(nid, ""),
                    ))
                diff.added_flows = [list(edge) for edge in current_fm.flows]
            else:
                # Compare frames
                old_set = set(old_fm.frames)
                new_set = set(current_fm.frames)
                current_names = _frame_names_from_body(current_md)
                old_names = _frame_names_from_body(old_md)

                for nid in sorted(new_set - old_set):
                    diff.added_frames.append(FrameChange(
                        node_id=nid, name=current_names.get(nid, ""),
                    ))
                for nid in sorted(old_set - new_set):
                    diff.removed_frames.append(FrameChange(
                        node_id=nid, name=old_names.get(nid, ""),
                    ))

                # Detect renames (same node_id, different name)
                for nid in sorted(old_set & new_set):
                    old_name = old_names.get(nid, "")
                    new_name = current_names.get(nid, "")
                    if old_name and new_name and old_name != new_name:
                        diff.renamed_frames.append(FrameRename(
                            node_id=nid, old_name=old_name, new_name=new_name,
                        ))

                # Compare flows
                old_flows = {tuple(e) for e in old_fm.flows if len(e) == 2}
                new_flows = {tuple(e) for e in current_fm.flows if len(e) == 2}
                for edge in sorted(new_flows - old_flows):
                    diff.added_flows.append(list(edge))
                for edge in sorted(old_flows - new_flows):
                    diff.removed_flows.append(list(edge))

        if diff.has_changes:
            diffs.append(diff)

    return diffs, since_date, until_date


# ── Output formatting ───────────────────────────────────────────────


def _format_text(diffs: list[PageDiff], since_date: datetime, until_date: datetime) -> str:
    """Render a human-readable report."""
    since_str = since_date.strftime("%b %d, %Y")
    until_str = until_date.strftime("%b %d, %Y")
    lines: list[str] = [f"Figma changes ({since_str} \u2013 {until_str})", ""]

    new_pages = [d for d in diffs if d.is_new]
    modified = [d for d in diffs if not d.is_new]

    if new_pages:
        lines.append("## New Pages")
        for d in new_pages:
            lines.append(f"  + {d.path} ({d.total_frames} frames)")
        lines.append("")

    if modified:
        lines.append("## Modified Pages")
        lines.append("")
        for d in modified:
            lines.append(f"### {d.path}")
            parts: list[str] = []
            if d.added_frames:
                parts.append(f"+{len(d.added_frames)} added")
            if d.removed_frames:
                parts.append(f"-{len(d.removed_frames)} removed")
            if d.renamed_frames:
                parts.append(f"{len(d.renamed_frames)} renamed")
            if parts:
                lines.append(f"  Frames: {', '.join(parts)}")
                for f in d.added_frames:
                    name_suffix = f"  {f.name}" if f.name else ""
                    lines.append(f"    + {f.node_id}{name_suffix}")
                for f in d.removed_frames:
                    name_suffix = f"  {f.name}" if f.name else ""
                    lines.append(f"    - {f.node_id}{name_suffix}")
                for r in d.renamed_frames:
                    lines.append(f"    ~ {r.node_id}  {r.old_name!r} -> {r.new_name!r}")

            flow_parts: list[str] = []
            if d.added_flows:
                flow_parts.append(f"+{len(d.added_flows)} new connections")
            if d.removed_flows:
                flow_parts.append(f"-{len(d.removed_flows)} removed connections")
            if flow_parts:
                lines.append(f"  Flows: {', '.join(flow_parts)}")
                for edge in d.added_flows:
                    lines.append(f"    + {edge[0]} \u2192 {edge[1]}")
                for edge in d.removed_flows:
                    lines.append(f"    - {edge[0]} \u2192 {edge[1]}")
            lines.append("")

    if not new_pages and not modified:
        lines.append("No design changes detected.")
        lines.append("")

    return "\n".join(lines)


def _format_json(diffs: list[PageDiff], since_date: datetime, until_date: datetime) -> str:
    """Render a machine-readable JSON report."""
    new_pages = []
    modified_pages = []

    for d in diffs:
        entry = {
            "path": d.path,
            "file_key": d.file_key,
            "page_node_id": d.page_node_id,
            "added_frames": [{"node_id": f.node_id, "name": f.name} for f in d.added_frames],
            "removed_frames": [{"node_id": f.node_id, "name": f.name} for f in d.removed_frames],
            "renamed_frames": [
                {"node_id": r.node_id, "old_name": r.old_name, "new_name": r.new_name}
                for r in d.renamed_frames
            ],
            "added_flows": d.added_flows,
            "removed_flows": d.removed_flows,
        }
        if d.is_new:
            entry["total_frames"] = d.total_frames
            new_pages.append(entry)
        else:
            modified_pages.append(entry)

    output = {
        "since": since_date.strftime("%Y-%m-%d"),
        "until": until_date.strftime("%Y-%m-%d"),
        "new_pages": new_pages,
        "modified_pages": modified_pages,
    }
    return json.dumps(output, indent=2)


# ── Click command ───────────────────────────────────────────────────


@click.command("diff")
@click.argument("target", default="figma/", required=False)
@click.option(
    "--since", default="7d", show_default=True,
    help="How far back to look (e.g. '7d', '14d', '1m').",
)
@click.option(
    "--format", "fmt", type=click.Choice(["text", "json"]), default="text",
    show_default=True,
    help="Output format.",
)
@click.pass_context
def diff_cmd(ctx: click.Context, target: str, since: str, fmt: str) -> None:
    """Show what designers changed in Figma by analyzing git history.

    Surfaces structural design changes (new pages, added/removed frames,
    flow changes) while filtering out enrichment-only changes.

    TARGET is the directory to scan (default: figma/).
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    resolved_target = (repo_dir / target).as_posix()
    # Use relative target for git log
    try:
        rel_target = str(Path(resolved_target).relative_to(repo_dir))
    except ValueError:
        rel_target = target

    diffs, since_date, until_date = compute_diff(repo_dir, rel_target, since)

    if fmt == "json":
        click.echo(_format_json(diffs, since_date, until_date))
    else:
        click.echo(_format_text(diffs, since_date, until_date))

    sys.exit(0)
