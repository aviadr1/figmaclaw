"""figmaclaw diff — show what designers actually changed in Figma.

Compares the Figma file tree at two points in time using the Figma
Versions API, then reports structural changes: new/removed frames,
renames, and flow changes.

This is the **only reliable way** to detect design changes — the git
history of .md files conflates initial sync, enrichment, and real
designer work.  The Figma API is the source of truth.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import click

from figmaclaw.figma_api_models import VersionSummary
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_models import FigmaFrame, from_page_node
from figmaclaw.figma_parse import parse_frontmatter

# ── Duration parsing ───────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)
_DURATION_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


def _parse_duration(since: str) -> timedelta:
    m = _DURATION_RE.match(since.strip())
    if not m:
        raise click.BadParameter(
            f"Cannot parse duration {since!r}. Use e.g. '7d', '2w', '1m'.",
            param_hint="--since",
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    return timedelta(days=_DURATION_DAYS[unit] * n)


# ── Data structures ────────────────────────────────────────────────


@dataclass
class VersionInfo:
    id: str
    created_at: str
    label: str
    user: str


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
    page_node_id: str
    page_name: str
    file_key: str
    figma_url: str = ""
    frames_before: int = 0
    frames_after: int = 0
    added_frames: list[FrameChange] = field(default_factory=list)
    removed_frames: list[FrameChange] = field(default_factory=list)
    renamed_frames: list[FrameRename] = field(default_factory=list)
    added_flows: list[list[str]] = field(default_factory=list)
    removed_flows: list[list[str]] = field(default_factory=list)
    is_new_page: bool = False

    @property
    def has_changes(self) -> bool:
        return (
            self.is_new_page
            or bool(self.added_frames)
            or bool(self.removed_frames)
            or bool(self.renamed_frames)
            or bool(self.added_flows)
            or bool(self.removed_flows)
        )


@dataclass
class FileDiff:
    file_key: str
    file_name: str
    old_version: VersionInfo | None
    new_version: VersionInfo | None
    versions_in_range: list[VersionInfo]
    pages: list[PageDiff]


# ── Extract frames from Figma page node ────────────────────────────


def _extract_frames(page_node: dict, file_key: str) -> tuple[
    dict[str, FigmaFrame], list[tuple[str, str]],
]:
    """Parse a CANVAS node and return (frames_by_id, flow_edges)."""
    if not page_node:
        return {}, []
    page = from_page_node(page_node, file_key=file_key, file_name="")
    frames: dict[str, FigmaFrame] = {}
    for section in page.sections:
        for f in section.frames:
            frames[f.node_id] = f
    return frames, list(page.flows)


# ── Core logic ─────────────────────────────────────────────────────


def _parse_version_ts(v: VersionSummary) -> datetime | None:
    try:
        return datetime.fromisoformat(v.created_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


async def _find_version_before(
    client: FigmaClient, file_key: str, cutoff: datetime,
) -> tuple[VersionInfo | None, list[VersionInfo]]:
    """Find the latest version before *cutoff* and all versions after it.

    Returns (old_version_or_none, versions_in_range).

    Uses pagination with early termination: stops fetching as soon as a
    version older than the cutoff is found.
    """
    def _is_before_cutoff(v: VersionSummary) -> bool:
        ts = _parse_version_ts(v)
        return ts is not None and ts < cutoff

    raw_versions = await client.get_versions(file_key, stop_when=_is_before_cutoff)

    old_version: VersionInfo | None = None
    in_range: list[VersionInfo] = []

    for v in raw_versions:
        ts = _parse_version_ts(v)
        if ts is None:
            continue
        vi = VersionInfo(
            id=v.id,
            created_at=v.created_at,
            label=v.label or "",
            user=v.user.handle,
        )
        if ts < cutoff:
            if old_version is None:
                old_version = vi
            break
        in_range.append(vi)

    in_range.reverse()  # oldest first
    return old_version, in_range


def _extract_all_pages(file_tree: dict, file_key: str) -> dict[str, dict]:
    """Extract {page_node_id: canvas_node} from a file tree response."""
    pages: dict[str, dict] = {}
    document = file_tree.get("document", {})
    for child in document.get("children", []):
        if child.get("type") == "CANVAS":
            pages[child["id"]] = child
    return pages


async def _diff_file(
    client: FigmaClient,
    file_key: str,
    file_name: str,
    tracked_page_ids: list[str],
    old_version: VersionInfo | None,
    versions_in_range: list[VersionInfo],
) -> FileDiff:
    """Compare tracked pages in a file between old_version and current HEAD.

    Uses ``get_file_shallow`` (depth=2) to fetch only the top-level
    frame/section structure, not the full recursive tree. This keeps
    response size small even for files with hundreds of deeply nested layers.
    """
    pages: list[PageDiff] = []
    new_version = versions_in_range[-1] if versions_in_range else None

    # 1 API call for current state (shallow — depth=2)
    current_tree = await client.get_file_shallow(file_key)
    current_pages = _extract_all_pages(current_tree, file_key)

    if old_version:
        try:
            old_tree = await client.get_file_shallow(
                file_key, version=old_version.id,
            )
            old_pages = _extract_all_pages(old_tree, file_key)
        except Exception:
            old_pages = {}
    else:
        old_pages = {}

    # Only diff tracked pages (+ any new pages not yet tracked)
    all_page_ids = set(tracked_page_ids) | set(current_pages) | set(old_pages)

    for page_id in sorted(all_page_ids):
        current_node = current_pages.get(page_id, {})
        old_node = old_pages.get(page_id, {})

        cur_frames, cur_flows = _extract_frames(current_node, file_key)
        old_frames, old_flows = _extract_frames(old_node, file_key)

        page_name = (
            current_node.get("name")
            or old_node.get("name")
            or page_id
        )

        diff = PageDiff(
            page_node_id=page_id,
            page_name=page_name,
            file_key=file_key,
            figma_url=f"https://www.figma.com/design/{file_key}?node-id={page_id.replace(':', '-')}",
            frames_before=len(old_frames),
            frames_after=len(cur_frames),
            is_new_page=bool(current_node) and not old_node,
        )

        old_ids = set(old_frames)
        cur_ids = set(cur_frames)

        for nid in sorted(cur_ids - old_ids):
            f = cur_frames[nid]
            diff.added_frames.append(FrameChange(node_id=nid, name=f.name))
        for nid in sorted(old_ids - cur_ids):
            f = old_frames[nid]
            diff.removed_frames.append(FrameChange(node_id=nid, name=f.name))
        for nid in sorted(old_ids & cur_ids):
            old_name = old_frames[nid].name
            new_name = cur_frames[nid].name
            if old_name != new_name:
                diff.renamed_frames.append(FrameRename(
                    node_id=nid, old_name=old_name, new_name=new_name,
                ))

        old_flow_set = set(old_flows)
        cur_flow_set = set(cur_flows)
        for edge in sorted(cur_flow_set - old_flow_set):
            diff.added_flows.append(list(edge))
        for edge in sorted(old_flow_set - cur_flow_set):
            diff.removed_flows.append(list(edge))

        if diff.has_changes:
            pages.append(diff)

    return FileDiff(
        file_key=file_key,
        file_name=file_name,
        old_version=old_version,
        new_version=new_version,
        versions_in_range=versions_in_range,
        pages=pages,
    )


async def _run(
    api_key: str,
    target: Path,
    since: str,
    progress: bool = False,
) -> tuple[list[FileDiff], datetime, datetime]:
    """Main async entry point. Scans .md files to discover tracked Figma files,
    then uses the Figma API to compute real diffs.

    Strategy (minimizes API calls):
    1. Discover tracked files from .md frontmatter
    2. Pre-filter with get_file_meta (cheap, returns lastModified)
    3. Fetch versions only for recently modified files
    4. For each active file, fetch tracked pages at current + old version
    5. Compare in memory — no more API calls
    """
    delta = _parse_duration(since)
    now = datetime.now(timezone.utc)
    cutoff = now - delta

    # Discover tracked files from .md frontmatter
    file_pages: dict[str, tuple[str, list[str]]] = {}  # file_key → (file_name, [page_ids])
    for md_path in sorted(target.rglob("*.md")):
        content = md_path.read_text()
        fm = parse_frontmatter(content)
        if fm is None or not fm.file_key:
            continue
        fk = fm.file_key
        if fk not in file_pages:
            rel = md_path.relative_to(target)
            file_name = rel.parts[0] if rel.parts else ""
            file_pages[fk] = (file_name, [])
        if fm.page_node_id and fm.page_node_id not in file_pages[fk][1]:
            file_pages[fk][1].append(fm.page_node_id)

    if progress:
        click.echo(f"Discovered {len(file_pages)} tracked Figma files", err=True)

    results: list[FileDiff] = []

    async with FigmaClient(api_key) as client:
        # Phase 0: pre-filter with get_file_meta (cheap — returns lastModified)
        if progress:
            click.echo(f"Checking lastModified for {len(file_pages)} files...", err=True)

        meta_tasks = [client.get_file_meta(fk) for fk in file_pages]
        meta_results = await asyncio.gather(*meta_tasks, return_exceptions=True)

        candidates: list[str] = []
        for fk, meta in zip(file_pages, meta_results):
            if isinstance(meta, BaseException):
                candidates.append(fk)  # if meta fails, still check versions
                continue
            try:
                last_mod = datetime.fromisoformat(
                    meta.lastModified.replace("Z", "+00:00"),
                )
                if last_mod >= cutoff:
                    candidates.append(fk)
            except (ValueError, AttributeError):
                candidates.append(fk)

        if progress:
            skipped = len(file_pages) - len(candidates)
            click.echo(
                f"  {len(candidates)} recently modified, {skipped} unchanged (skipped)",
                err=True,
            )

        # Phase 1: find active files (versions lookup — only candidates)
        if progress and candidates:
            click.echo(f"Fetching version history for {len(candidates)} files...", err=True)

        version_tasks = [
            _find_version_before(client, fk, cutoff)
            for fk in candidates
        ]
        version_results = await asyncio.gather(*version_tasks, return_exceptions=True)

        active_files: list[tuple[str, str, list[str], VersionInfo | None, list[VersionInfo]]] = []
        for fk, vr in zip(candidates, version_results):
            file_name, page_ids = file_pages[fk]
            if isinstance(vr, BaseException):
                if progress:
                    click.echo(
                        f"  WARNING: version lookup failed for {file_name} ({fk}): "
                        f"{type(vr).__name__}: {vr}",
                        err=True,
                    )
                continue
            old_ver, in_range = vr
            if in_range:
                active_files.append((fk, file_name, page_ids, old_ver, in_range))

        if progress:
            click.echo(f"  {len(active_files)} files had activity in the window", err=True)

        # Phase 2: diff active files (per-page fetches — 2 API calls per file)
        if active_files:
            if progress:
                total_pages = sum(len(pids) for _, _, pids, _, _ in active_files)
                click.echo(
                    f"Fetching pages for {len(active_files)} active files "
                    f"({total_pages} tracked pages)...",
                    err=True,
                )

            diff_tasks = [
                asyncio.wait_for(
                    _diff_file(client, fk, fn, pids, old_ver, in_range),
                    timeout=300,  # 5 min per file
                )
                for fk, fn, pids, old_ver, in_range in active_files
            ]
            diffs = await asyncio.gather(*diff_tasks, return_exceptions=True)

            for (fk, fn, _, _, _), d in zip(active_files, diffs):
                if isinstance(d, BaseException):
                    click.echo(
                        f"  WARNING: failed to diff {fn} ({fk}): "
                        f"{type(d).__name__}: {d}",
                        err=True,
                    )
                    continue
                if d.pages:
                    results.append(d)
                elif progress:
                    click.echo(
                        f"  {fn}: {len(d.versions_in_range)} versions but no "
                        f"structural frame changes (edits only)",
                        err=True,
                    )

    return results, cutoff, now


# ── Output formatting ──────────────────────────────────────────────


def _format_text(
    results: list[FileDiff], since_date: datetime, until_date: datetime,
) -> str:
    since_str = since_date.strftime("%b %d, %Y")
    until_str = until_date.strftime("%b %d, %Y")
    lines: list[str] = [f"Figma design changes ({since_str} \u2013 {until_str})", ""]

    for fd in results:
        ver_count = len(fd.versions_in_range)
        users = sorted({v.user for v in fd.versions_in_range if v.user})
        user_str = ", ".join(users) if users else "unknown"
        lines.append(f"## {fd.file_name} ({ver_count} version{'s' if ver_count != 1 else ''} by {user_str})")
        if fd.old_version:
            lines.append(f"  Comparing: {fd.old_version.created_at[:16]} \u2192 now")
        else:
            lines.append(f"  No version before window \u2014 showing all current frames")
        lines.append("")

        for p in fd.pages:
            lines.append(f"### {p.page_name}")
            lines.append(f"  \U0001f4d0 {p.figma_url}")
            if p.is_new_page:
                lines.append(f"  NEW PAGE ({p.frames_after} frames)")
            else:
                lines.append(f"  Frames: {p.frames_before} \u2192 {p.frames_after}")

            parts: list[str] = []
            if p.added_frames:
                parts.append(f"+{len(p.added_frames)} added")
            if p.removed_frames:
                parts.append(f"-{len(p.removed_frames)} removed")
            if p.renamed_frames:
                parts.append(f"{len(p.renamed_frames)} renamed")
            if parts:
                lines.append(f"  Changes: {', '.join(parts)}")
                for f in p.added_frames:
                    name_sfx = f"  {f.name}" if f.name else ""
                    lines.append(f"    + {f.node_id}{name_sfx}")
                for f in p.removed_frames:
                    name_sfx = f"  {f.name}" if f.name else ""
                    lines.append(f"    - {f.node_id}{name_sfx}")
                for r in p.renamed_frames:
                    lines.append(f"    ~ {r.node_id}  {r.old_name!r} \u2192 {r.new_name!r}")

            flow_parts: list[str] = []
            if p.added_flows:
                flow_parts.append(f"+{len(p.added_flows)} new")
            if p.removed_flows:
                flow_parts.append(f"-{len(p.removed_flows)} removed")
            if flow_parts:
                lines.append(f"  Flows: {', '.join(flow_parts)}")

            lines.append("")

        # Version timeline
        lines.append(f"  Versions:")
        for v in fd.versions_in_range:
            label = f"  \"{v.label}\"" if v.label else ""
            lines.append(f"    {v.created_at[:16]}  {v.user}{label}")
        lines.append("")

    if not results:
        lines.append("No design changes detected in any tracked Figma file.")
        lines.append("")

    return "\n".join(lines)


def _format_json(
    results: list[FileDiff], since_date: datetime, until_date: datetime,
) -> str:
    output: dict[str, Any] = {
        "since": since_date.strftime("%Y-%m-%d"),
        "until": until_date.strftime("%Y-%m-%d"),
        "files": [],
    }
    for fd in results:
        file_entry: dict[str, Any] = {
            "file_key": fd.file_key,
            "file_name": fd.file_name,
            "versions_in_range": [
                {"id": v.id, "created_at": v.created_at, "label": v.label, "user": v.user}
                for v in fd.versions_in_range
            ],
            "pages": [],
        }
        for p in fd.pages:
            page_entry: dict[str, Any] = {
                "page_node_id": p.page_node_id,
                "page_name": p.page_name,
                "figma_url": p.figma_url,
                "is_new_page": p.is_new_page,
                "frames_before": p.frames_before,
                "frames_after": p.frames_after,
                "added_frames": [{"node_id": f.node_id, "name": f.name} for f in p.added_frames],
                "removed_frames": [{"node_id": f.node_id, "name": f.name} for f in p.removed_frames],
                "renamed_frames": [
                    {"node_id": r.node_id, "old_name": r.old_name, "new_name": r.new_name}
                    for r in p.renamed_frames
                ],
                "added_flows": p.added_flows,
                "removed_flows": p.removed_flows,
            }
            file_entry["pages"].append(page_entry)
        output["files"].append(file_entry)

    return json.dumps(output, indent=2)


# ── Click command ──────────────────────────────────────────────────


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
@click.option(
    "--progress/--no-progress", default=True,
    help="Show progress to stderr while fetching from the Figma API.",
)
@click.pass_context
def diff_cmd(
    ctx: click.Context, target: str, since: str, fmt: str, progress: bool,
) -> None:
    """Show what designers changed in Figma using the Figma Versions API.

    Compares Figma file trees at two points in time to detect structural
    design changes: new/removed frames, renames, and flow changes.

    Requires FIGMA_API_KEY environment variable.

    TARGET is the directory with tracked .md files (default: figma/).
    """
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.ClickException("FIGMA_API_KEY environment variable is not set.")

    repo_dir = Path(ctx.obj["repo_dir"])
    resolved = repo_dir / target
    if not resolved.is_dir():
        raise click.ClickException(f"Target directory not found: {resolved}")

    results, since_date, until_date = asyncio.run(
        _run(api_key, resolved, since, progress=progress),
    )

    if fmt == "json":
        click.echo(_format_json(results, since_date, until_date))
    else:
        click.echo(_format_text(results, since_date, until_date))
