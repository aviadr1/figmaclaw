"""figmaclaw census — snapshot published component sets for tracked Figma files.

Uses the team-level component-set listing when a team ID is available, falling
back to GET /v1/files/{file_key}/component_sets per file. Writes a `_census.md`
file alongside each file's pages/ directory. The file is only rewritten when
the content hash changes, keeping diffs meaningful.

Output: figma/{file_slug}/_census.md

Frontmatter fields:
  file_key          — Figma file key
  generated_at      — ISO 8601 UTC timestamp of this run
  content_hash      — 16-char stable hash of sorted (name, key) pairs.
                      Changes when components are added, removed, or renamed.
                      Unchanged when component content changes (thumbnail, etc.)
  component_set_count — total published component sets in this file

Body: markdown table of all published component sets, sorted alphabetically.
Columns: Component set | Key | Page | Updated

Why content hash not timestamp:
  The hash lets CI and agents detect "did the DS registry change?" separately
  from "did anything in the file get touched?". Only registry changes (add /
  remove / rename a component set) should trigger re-audit. The generated_at
  timestamp covers staleness checking independently.

Hash skip:
  If the existing _census.md already has `content_hash: {current_hash}` in its
  frontmatter, the file is not rewritten and no commit is emitted. Only real
  registry changes trigger a write + commit, keeping git history signal-rich.

Integration:
  Run after `figmaclaw pull` in CI:
    figmaclaw census --auto-commit
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

from figmaclaw.commands._shared import load_state, require_figma_api_key, require_tracked_files
from figmaclaw.commands.observability import (
    StructuredObs,
    async_heartbeat_loop,
    env_interval_seconds,
)
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_paths import census_path, file_slug_for_key
from figmaclaw.git_utils import git_commit
from figmaclaw.source_context import SourceContext, source_context_from_manifest_entry
from figmaclaw.status_markers import COMMIT_MSG_PREFIX

# ── Hash ─────────────────────────────────────────────────────────────────────


def _compute_hash(component_sets: list[dict[str, Any]]) -> str:
    """Stable 16-char hash of the published component set registry.

    Hashes sorted (name, key) pairs only. Changes when a component set is
    added, removed, renamed, or its Figma key changes. Does NOT change when
    component content (thumbnail, internal variants) changes.
    """
    pairs = sorted(
        (cs.get("name", ""), cs.get("key", ""))
        for cs in component_sets
        if cs.get("name") and cs.get("key")
    )
    canonical = json.dumps(pairs, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render(
    file_key: str,
    file_name: str,
    component_sets: list[dict[str, Any]],
    content_hash: str,
    generated_at: str,
    source_context: SourceContext | None = None,
) -> str:
    """Render the census markdown content."""
    sorted_sets = sorted(component_sets, key=lambda cs: cs.get("name", "").lower())
    source_context = source_context or SourceContext()

    fm: dict[str, Any] = {
        "file_key": file_key,
        "generated_at": generated_at,
        "content_hash": content_hash,
        "component_set_count": len(sorted_sets),
    }
    if source_context.project_id:
        fm["source_project_id"] = source_context.project_id
    if source_context.project_name:
        fm["source_project_name"] = source_context.project_name
    if source_context.lifecycle != "unknown":
        fm["source_lifecycle"] = source_context.lifecycle
    # Sort keys for stable diffs; use block style (no flow) since values are all scalars.
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=True).strip()

    lines: list[str] = [f"---\n{fm_yaml}\n---", ""]
    lines.append(f"# {file_name} — Published Component Sets\n")
    lines.append("| Component set | Key | Page | Updated |")
    lines.append("|---|---|---|---|")

    for cs in sorted_sets:
        name = cs.get("name", "")
        key = cs.get("key", "")
        containing = cs.get("containing_frame", {})
        page = containing.get("pageName", "—")
        updated_raw = cs.get("updated_at", "")
        updated = updated_raw[:10] if updated_raw else "—"
        lines.append(f"| `{name}` | `{key}` | {page} | {updated} |")

    return "\n".join(lines) + "\n"


# ── Hash check (fast, no YAML parse needed) ───────────────────────────────────


def _existing_hash(path: Path) -> str | None:
    """Extract content_hash from existing _census.md frontmatter, or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("content_hash:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _existing_source_context_matches(path: Path, source_context: SourceContext) -> bool:
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return False
        _start, fm_text, _rest = text.split("---", 2)
        data = yaml.safe_load(fm_text) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return (
        data.get("source_project_id") == source_context.project_id
        and data.get("source_project_name") == source_context.project_name
        and (data.get("source_lifecycle") or "unknown") == source_context.lifecycle
    )


def load_census_registry(path: Path) -> dict[str, str]:
    """Read component-set key/name pairs from a figmaclaw ``_census.md`` file."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = _strip_code_cell(cells[0])
        key = _strip_code_cell(cells[1])
        if name and key:
            result[key] = name
    return result


def _strip_code_cell(value: str) -> str:
    value = value.strip()
    if value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value


# ── Command ───────────────────────────────────────────────────────────────────


def _census_heartbeat_seconds() -> int:
    return env_interval_seconds("FIGMACLAW_CENSUS_HEARTBEAT_SECONDS", 30)


@click.command("census")
@click.option(
    "--file-key",
    "file_key",
    default=None,
    help="Census only this file key (default: all tracked files).",
)
@click.option(
    "--auto-commit", "auto_commit", is_flag=True, help="git commit each written _census.md."
)
@click.option("--force", is_flag=True, help="Write even if content hash is unchanged.")
@click.option(
    "--team-id",
    "team_id",
    default=None,
    envvar="FIGMA_TEAM_ID",
    help="Figma team ID. Enables one team-level component-set scan instead of per-file scans.",
)
@click.pass_context
def census_cmd(
    ctx: click.Context,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
    team_id: str | None,
) -> None:
    """Snapshot published component sets for all tracked files.

    Writes figma/{file-slug}/_census.md for each file that has published
    component sets. With --file-key, also writes an explicit empty census
    when the file has zero published sets. Only updates the file when the
    component registry changes.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()

    asyncio.run(_run(api_key, repo_dir, file_key, auto_commit, force, team_id))


async def _run(
    api_key: str,
    repo_dir: Path,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
    team_id: str | None = None,
) -> None:
    state = load_state(repo_dir)
    if not require_tracked_files(state):
        return

    keys = [file_key] if file_key else list(state.manifest.tracked_files)
    written: list[str] = []
    obs = StructuredObs("SYNC_OBS_CENSUS")
    heartbeat_interval_s = _census_heartbeat_seconds()
    obs.emit("run_start", files_seen=len(keys), force=force, single_file=bool(file_key))

    try:
        async with FigmaClient(api_key) as client:
            team_component_sets_by_file: dict[str, list[dict[str, Any]]] | None = None
            if team_id and not file_key:
                reader_start = time.monotonic()
                try:
                    obs.emit("reader_start", reader="rest_team_component_sets", team_id=team_id)
                    team_component_sets = await client.list_team_component_sets(team_id)
                    team_component_sets_by_file = {}
                    for component_set in team_component_sets:
                        source_file_key = component_set.get("file_key")
                        if isinstance(source_file_key, str) and source_file_key:
                            team_component_sets_by_file.setdefault(source_file_key, []).append(
                                component_set
                            )
                    obs.emit(
                        "reader_end",
                        reader="rest_team_component_sets",
                        outcome="success",
                        component_sets=len(team_component_sets),
                        files_with_component_sets=len(team_component_sets_by_file),
                        duration_s=round(time.monotonic() - reader_start, 3),
                    )
                except Exception as exc:
                    click.echo(
                        "team component-set scan unavailable; falling back to per-file census "
                        f"— {exc}"
                    )
                    obs.emit(
                        "reader_end",
                        reader="rest_team_component_sets",
                        outcome="error",
                        error=type(exc).__name__,
                        duration_s=round(time.monotonic() - reader_start, 3),
                    )

            for key in keys:
                file_start = time.monotonic()
                stop_heartbeat = asyncio.Event()
                heartbeat_task = asyncio.create_task(
                    async_heartbeat_loop(
                        obs,
                        event="file_heartbeat",
                        start=file_start,
                        stop_event=stop_heartbeat,
                        interval_s=heartbeat_interval_s,
                        fields={"file_key": key},
                    )
                )
                obs.emit("file_start", file_key=key)
                try:
                    if key not in state.manifest.tracked_files:
                        click.echo(f"{key}: not tracked — skip")
                        obs.emit(
                            "file_end",
                            file_key=key,
                            outcome="not_tracked",
                            duration_s=round(time.monotonic() - file_start, 3),
                        )
                        continue

                    skip_reason = state.manifest.skipped_files.get(key)
                    if skip_reason:
                        click.echo(f"{key}: skipped — {skip_reason}")
                        obs.emit(
                            "file_end",
                            file_key=key,
                            outcome="manifest_skipped",
                            duration_s=round(time.monotonic() - file_start, 3),
                        )
                        continue

                    file_entry = state.manifest.files.get(key)
                    file_name = file_entry.file_name if file_entry else key
                    file_slug = file_slug_for_key(file_name, key)
                    source_context = source_context_from_manifest_entry(file_entry)

                    if team_component_sets_by_file is not None:
                        component_sets = team_component_sets_by_file.get(key, [])
                        obs.emit(
                            "reader_end",
                            file_key=key,
                            file_name=file_name,
                            reader="rest_team_component_sets_cache",
                            outcome="success",
                            component_sets=len(component_sets),
                            duration_s=0,
                        )
                    else:
                        reader_start = time.monotonic()
                        try:
                            obs.emit(
                                "reader_start",
                                file_key=key,
                                file_name=file_name,
                                reader="rest_component_sets",
                            )
                            component_sets = await client.get_component_sets(key)
                            obs.emit(
                                "reader_end",
                                file_key=key,
                                file_name=file_name,
                                reader="rest_component_sets",
                                outcome="success",
                                component_sets=len(component_sets),
                                duration_s=round(time.monotonic() - reader_start, 3),
                            )
                        except Exception as exc:
                            click.echo(f"{key} ({file_name}): failed — {exc}")
                            obs.emit(
                                "reader_end",
                                file_key=key,
                                file_name=file_name,
                                reader="rest_component_sets",
                                outcome="error",
                                error=type(exc).__name__,
                                duration_s=round(time.monotonic() - reader_start, 3),
                            )
                            obs.emit(
                                "file_end",
                                file_key=key,
                                file_name=file_name,
                                outcome="reader_error",
                                duration_s=round(time.monotonic() - file_start, 3),
                            )
                            continue

                    if not component_sets:
                        # No published component sets — skip by default (e.g. product files).
                        # For an explicitly requested file, persist the empty registry so
                        # repo readers can distinguish "probed empty" from "not probed".
                        # Canon: REG-1 (explicit registry state).
                        if not file_key:
                            obs.emit(
                                "file_end",
                                file_key=key,
                                file_name=file_name,
                                outcome="empty_skipped",
                                duration_s=round(time.monotonic() - file_start, 3),
                            )
                            continue
                        click.echo(f"{file_name}: 0 published component set(s)")

                    content_hash = _compute_hash(component_sets)
                    out_path = repo_dir / census_path(file_slug)

                    if (
                        not force
                        and _existing_hash(out_path) == content_hash
                        and _existing_source_context_matches(out_path, source_context)
                    ):
                        click.echo(f"{file_name}: census unchanged (hash {content_hash})")
                        obs.emit(
                            "file_end",
                            file_key=key,
                            file_name=file_name,
                            outcome="unchanged",
                            component_sets=len(component_sets),
                            duration_s=round(time.monotonic() - file_start, 3),
                        )
                        continue

                    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    content = _render(
                        key,
                        file_name,
                        component_sets,
                        content_hash,
                        generated_at,
                        source_context,
                    )

                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(content, encoding="utf-8")

                    # Enforce the round-trip invariant: _existing_hash must be able to read back
                    # the hash we just embedded. If this fires, _render and _existing_hash have
                    # drifted (e.g. the frontmatter field was renamed) and every subsequent run
                    # will rewrite the file, creating spurious git commits.
                    assert _existing_hash(out_path) == content_hash, (
                        f"BUG: wrote {out_path} but _existing_hash cannot recover content_hash={content_hash!r}. "
                        "The census skip check is broken — every future run will rewrite this file."
                    )

                    rel = census_path(file_slug)
                    click.echo(
                        f"{file_name}: wrote {len(component_sets)} component set(s)"
                        f" [hash {content_hash}] → {rel}"
                    )
                    written.append(rel)

                    if auto_commit:
                        committed = git_commit(
                            repo_dir, [rel], f"sync: figmaclaw census — {file_name}"
                        )
                        if committed:
                            click.echo("  ✓ committed")
                    obs.emit(
                        "file_end",
                        file_key=key,
                        file_name=file_name,
                        outcome="written",
                        component_sets=len(component_sets),
                        duration_s=round(time.monotonic() - file_start, 3),
                    )
                finally:
                    stop_heartbeat.set()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
    except Exception:
        obs.emit(
            "run_end",
            duration_s=obs.duration(),
            files_seen=len(keys),
            files_written=len(written),
            reason="error",
        )
        raise

    if written:
        click.echo(f"{COMMIT_MSG_PREFIX}sync: figmaclaw census — {len(written)} file(s) updated")
    obs.emit(
        "run_end",
        duration_s=obs.duration(),
        files_seen=len(keys),
        files_written=len(written),
    )
