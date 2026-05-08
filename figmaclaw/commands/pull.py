"""figmaclaw pull — incremental sync of all tracked Figma files."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import click

from figmaclaw.commands._shared import (
    figma_variables_api_key,
    load_state,
    require_figma_api_key,
    require_tracked_files,
)
from figmaclaw.commands.listing_prefilter import listing_prefilter
from figmaclaw.commands.observability import (
    StructuredObs,
    async_heartbeat_loop,
    env_interval_seconds,
)
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
from figmaclaw.figma_sync_state import file_has_pull_schema_debt
from figmaclaw.git_utils import git_commit, git_push
from figmaclaw.prune_utils import prune_file_artifacts_from_manifest
from figmaclaw.pull_logic import DEFAULT_PER_PAGE_TIMEOUT_S, PullResult, pull_file
from figmaclaw.status_markers import COMMIT_MSG_PREFIX, HAS_MORE_TRUE

DEFAULT_PER_FILE_TIMEOUT_S: float = 300.0


@click.command("pull")
@click.option("--file-key", "file_key", default=None, help="Pull only this file key.")
@click.option("--force", is_flag=True, help="Regenerate all pages even if hash is unchanged.")
@click.option(
    "--max-pages",
    "max_pages",
    default=None,
    type=int,
    help="Global page budget per run (batch loop mode).",
)
@click.option(
    "--auto-commit",
    "auto_commit",
    is_flag=True,
    help="git commit after each page. CI should do a final git push.",
)
@click.option(
    "--push-every",
    "push_every",
    default=10,
    type=int,
    show_default=True,
    help="Push every N commits when --auto-commit is set.",
)
@click.option(
    "--team-id",
    "team_id",
    default=None,
    envvar="FIGMA_TEAM_ID",
    help="Figma team ID. Enables fast listing pre-filter and auto-discovery of new files.",
)
@click.option(
    "--since",
    "since",
    default="3m",
    show_default=True,
    help="When --team-id is set, only track files modified within this window (e.g. 3m, 7d, all).",
)
@click.option(
    "--prune/--no-prune",
    "prune",
    default=True,
    show_default=True,
    help="Prune stale generated figma artifacts (orphans, old rename paths, removed pages).",
)
@click.option(
    "--per-page-timeout-s",
    "per_page_timeout_s",
    default=DEFAULT_PER_PAGE_TIMEOUT_S,
    type=float,
    show_default=True,
    help=(
        "Abort a single Figma API operation after this many seconds and "
        "mark the affected page/probe errored so the loop can continue. Pass 0 to disable."
    ),
)
@click.option(
    "--per-file-timeout-s",
    "per_file_timeout_s",
    default=DEFAULT_PER_FILE_TIMEOUT_S,
    type=float,
    show_default=True,
    help=(
        "Abort a single tracked file after this many seconds so one slow Figma file "
        "does not block the whole sync batch. Pass 0 to disable."
    ),
)
@click.pass_context
def pull_cmd(
    ctx: click.Context,
    file_key: str | None,
    force: bool,
    max_pages: int | None,
    auto_commit: bool,
    push_every: int,
    team_id: str | None,
    since: str,
    prune: bool,
    per_page_timeout_s: float,
    per_file_timeout_s: float,
) -> None:
    """Pull all tracked Figma files and write changed pages to disk."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()

    # 0 = caller opts out of per-page timeouts. Map to None to keep the signature
    # explicit in pull_logic.pull_file (None => no asyncio.wait_for wrapping).
    resolved_timeout: float | None = per_page_timeout_s if per_page_timeout_s > 0 else None
    resolved_file_timeout: float | None = per_file_timeout_s if per_file_timeout_s > 0 else None

    asyncio.run(
        _run(
            api_key,
            repo_dir,
            file_key,
            force,
            max_pages,
            auto_commit,
            push_every,
            team_id,
            since,
            prune=prune,
            per_page_timeout_s=resolved_timeout,
            per_file_timeout_s=resolved_file_timeout,
        )
    )


def _git_commit_page(repo_dir: Path, page_label: str) -> bool:
    """Stage figma/ and .figma-sync/, commit if anything changed. Returns True if committed."""
    return git_commit(repo_dir, ["figma/", ".figma-sync/"], f"sync: figmaclaw — {page_label}")


def _git_push(repo_dir: Path) -> None:
    git_push(repo_dir)


class _PullObs:
    """Structured pull observability emitter + counters."""

    def __init__(
        self,
        *,
        force: bool,
        max_pages: int | None,
        team_id: str | None,
        since: str,
        prune: bool,
    ) -> None:
        self.structured = StructuredObs("SYNC_OBS_PULL")
        self.files_seen = 0
        self.files_attempted_pull = 0
        self.files_skipped_prefilter = 0
        self.files_errors = 0
        self.files_no_access = 0
        self.files_updated = 0
        self.files_skipped = 0
        self.emit(
            "run_start",
            force=force,
            max_pages=max_pages if max_pages is not None else "none",
            team_id=team_id if team_id is not None else "none",
            since=since,
            prune=prune,
        )

    def emit(self, event: str, **fields: Any) -> None:
        self.structured.emit(event, **fields)

    def duration(self) -> float:
        return self.structured.duration()

    def set_files_seen(self, n: int) -> None:
        self.files_seen = n

    def file_end(self, file_key: str, outcome: str, file_start: float, **fields: Any) -> None:
        self.emit(
            "file_end",
            file_key=file_key,
            outcome=outcome,
            duration_s=round(time.monotonic() - file_start, 3),
            **fields,
        )

    def run_end(self, *, has_more_global: bool, reason: str | None = None) -> None:
        payload: dict[str, Any] = {
            "duration_s": self.duration(),
            "files_seen": self.files_seen,
            "files_attempted_pull": self.files_attempted_pull,
            "files_skipped_prefilter": self.files_skipped_prefilter,
            "files_errors": self.files_errors,
            "files_no_access": self.files_no_access,
            "files_updated": self.files_updated,
            "files_skipped": self.files_skipped,
            "has_more_global": has_more_global,
        }
        if reason is not None:
            payload["reason"] = reason
        self.emit("run_end", **payload)


def _pull_heartbeat_seconds() -> int:
    return env_interval_seconds("FIGMACLAW_PULL_HEARTBEAT_SECONDS", 30)


async def _file_heartbeat_loop(
    obs: _PullObs, *, file_key: str, file_start: float, stop_event: asyncio.Event, interval_s: int
) -> None:
    await async_heartbeat_loop(
        obs.structured,
        event="file_heartbeat",
        start=file_start,
        stop_event=stop_event,
        interval_s=interval_s,
        fields={"file_key": file_key},
    )


async def _run(
    api_key: str,
    repo_dir: Path,
    file_key: str | None,
    force: bool,
    max_pages: int | None,
    auto_commit: bool,
    push_every: int,
    team_id: str | None,
    since: str,
    prune: bool = True,
    per_page_timeout_s: float | None = None,
    per_file_timeout_s: float | None = DEFAULT_PER_FILE_TIMEOUT_S,
) -> None:
    state = load_state(repo_dir)

    commit_count = 0
    obs = _PullObs(
        force=force,
        max_pages=max_pages,
        team_id=team_id,
        since=since,
        prune=prune,
    )
    heartbeat_interval_s = _pull_heartbeat_seconds()

    def on_page_written(page_label: str, paths: list[str]) -> None:
        nonlocal commit_count
        if not auto_commit:
            return
        committed = _git_commit_page(repo_dir, page_label)
        if committed:
            commit_count += 1
            click.echo(f"  ✓ committed: {page_label}")
            if push_every and commit_count % push_every == 0:
                click.echo(f"  ↑ pushing ({commit_count} commits)...")
                _git_push(repo_dir)

    all_results: list[PullResult] = []
    pages_budget = max_pages
    has_more_global = False

    async with FigmaClient(api_key, variables_api_key=figma_variables_api_key(api_key)) as client:
        # Fast listing pre-filter: one listing pass replaces N individual get_file_meta
        # calls for unchanged files. Also handles auto-discovery when team_id is set.
        # None means "no listing available" (team_id not set); {} means "listing ran but
        # returned no files" — both are handled correctly by the is not None check below.
        listing_last_modified: dict[str, str] | None = None
        if team_id and not file_key:
            listing_t0 = time.monotonic()
            listing = await listing_prefilter(client, team_id, state, since)
            listing_last_modified = listing.last_modified_by_key
            state.save()  # persist any newly tracked files before pulling
            listing_duration_s = round(time.monotonic() - listing_t0, 3)
            obs.emit(
                "listing_prefilter",
                duration_s=listing_duration_s,
                listed_files=len(listing_last_modified),
                tracked_before=listing.tracked_before,
                tracked_after=listing.tracked_after,
            )

        if not require_tracked_files(state):
            obs.run_end(has_more_global=False, reason="no_tracked_files")
            return

        keys = [file_key] if file_key else list(state.manifest.tracked_files)
        obs.set_files_seen(len(keys))

        for key in keys:
            file_start = time.monotonic()
            if key not in state.manifest.tracked_files:
                click.echo(f"File key {key!r} is not tracked. Run 'figmaclaw track {key}' first.")
                obs.files_skipped += 1
                obs.file_end(key, "not_tracked", file_start)
                continue

            skip_reason = state.manifest.skipped_files.get(key)
            if skip_reason:
                click.echo(f"{key}: skipped — {skip_reason}")
                obs.files_skipped += 1
                obs.file_end(key, "manifest_skipped", file_start)
                continue

            if max_pages is not None and pages_budget is not None and pages_budget <= 0:
                has_more_global = True
                obs.file_end(key, "budget_exhausted", file_start)
                break

            stored_entry = state.manifest.files.get(key)
            file_name = getattr(stored_entry, "file_name", "") or "unknown"
            obs.emit("file_start", file_key=key, file_name=file_name)

            # Listing pre-filter: skip get_file_meta only when the listing proves
            # the tracked file is still present and unchanged.
            #   - listing_lm is None  → file not in team listing; probe with get_file_meta
            #     instead of skipping. Absence can mean deleted, moved, permission-lost,
            #     or an API listing gap; only the file-scoped endpoint can distinguish
            #     "still accessible" from "must prune artifacts".
            #   - listing_lm == stored → last_modified unchanged; skip
            #   - listing_lm != stored → file changed; proceed to get_file_meta
            #
            # Exception (figmaclaw#123): a tracked file whose
            # ``pull_schema_version`` is below CURRENT must be pulled even
            # if Figma reports it unchanged — otherwise schema bumps never
            # reach files that are idle on the Figma side. Before this
            # escape hatch, the linear-git showcase-v2 stuck case sat in
            # a listing-match skip forever and the v7 refresh never fired.
            if not force and listing_last_modified is not None:
                listing_lm = listing_last_modified.get(key)
                stored_lm = stored_entry.last_modified if stored_entry else ""
                schema_needs_refresh = (
                    file_has_pull_schema_debt(
                        stored_entry,
                        current_pull_schema_version=CURRENT_PULL_SCHEMA_VERSION,
                        should_skip_page=state.should_skip_page,
                    )
                    if stored_entry is not None
                    else True
                )
                unchanged_on_figma = bool(listing_lm and stored_lm == listing_lm)
                if unchanged_on_figma and not schema_needs_refresh:
                    obs.files_skipped_prefilter += 1
                    obs.file_end(key, "listing_prefilter_skip", file_start)
                    continue
                if listing_lm is None:
                    obs.emit("listing_prefilter_probe", file_key=key, reason="missing_from_listing")

            try:
                obs.files_attempted_pull += 1
                stop_heartbeat = asyncio.Event()
                heartbeat_task = asyncio.create_task(
                    _file_heartbeat_loop(
                        obs,
                        file_key=key,
                        file_start=file_start,
                        stop_event=stop_heartbeat,
                        interval_s=heartbeat_interval_s,
                    )
                )
                try:
                    pull_one = pull_file(
                        client,
                        key,
                        state,
                        repo_dir,
                        force=force,
                        max_pages=pages_budget,
                        prune=prune,
                        on_page_written=on_page_written,
                        per_page_timeout_s=per_page_timeout_s,
                    )
                    if per_file_timeout_s is None:
                        result = await pull_one
                    else:
                        result = await asyncio.wait_for(pull_one, timeout=per_file_timeout_s)
                finally:
                    stop_heartbeat.set()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
            except TimeoutError:
                timeout_label = (
                    f"{per_file_timeout_s}s" if per_file_timeout_s is not None else "unknown"
                )
                click.echo(f"{key}: timed out after {timeout_label} (skipping)")
                obs.files_errors += 1
                obs.file_end(
                    key,
                    "file_timeout",
                    file_start,
                    timeout_s=per_file_timeout_s if per_file_timeout_s is not None else "none",
                )
                continue
            except Exception as exc:
                click.echo(f"{key}: error — {exc} (skipping)")
                obs.files_errors += 1
                obs.file_end(key, "error", file_start)
                continue
            all_results.append(result)

            if max_pages is not None and pages_budget is not None:
                # Schema-only upgrades don't count toward the budget — they can't cause
                # infinite loops and always complete in a single pass regardless of max_pages.
                pages_budget -= result.pages_written
            if result.has_more:
                has_more_global = True

            if result.no_access:
                obs.files_no_access += 1
                # Permanently inaccessible (restricted/deleted) — move out of tracked_files.
                reason = "no access — get_file_meta returns 400/404"
                pruned = prune_file_artifacts_from_manifest(
                    state,
                    repo_dir,
                    key,
                    drop_manifest_entry=True,
                    drop_tracked=True,
                )
                state.manifest.skipped_files[key] = reason
                click.echo(
                    f"{key}: skipped — {reason} — removed from tracked_files "
                    f"(pruned {pruned} path(s))"
                )
                obs.file_end(key, "no_access_pruned", file_start, pruned_paths=pruned)
            elif result.skipped_file:
                obs.files_skipped += 1
                # If pull failed (e.g. 400 on get_file_meta) and we know the listing
                # last_modified, stamp it into the manifest so future runs pre-filter
                # this file without making a wasted API call.
                if listing_last_modified is not None:
                    listing_lm = listing_last_modified.get(key)
                    stored_entry = state.manifest.files.get(key)
                    if listing_lm and stored_entry and not stored_entry.last_modified:
                        stored_entry.last_modified = listing_lm
                click.echo(f"{key}: unchanged (skipped)")
                obs.file_end(key, "pull_skipped", file_start)
            else:
                wrote_any = bool(
                    result.pages_written
                    or result.component_sections_written
                    or result.pages_schema_upgraded
                )
                if wrote_any:
                    obs.files_updated += 1
                    outcome = "updated"
                else:
                    # Pull processed file-level metadata but wrote no page/component output.
                    outcome = "processed_no_writes"
                errored = f", {result.pages_errored} error(s)" if result.pages_errored else ""
                upgraded = (
                    f", {result.pages_schema_upgraded} schema-upgraded"
                    if result.pages_schema_upgraded
                    else ""
                )
                click.echo(
                    f"{key}: wrote {result.pages_written} page(s), {result.component_sections_written} component(s), skipped {result.pages_skipped}{upgraded}{errored}"
                )
                for path in result.md_paths:
                    click.echo(f"  → {path}")
                for path in result.component_paths:
                    click.echo(f"  ❖ {path}")
                obs.file_end(
                    key,
                    outcome,
                    file_start,
                    pages_written=result.pages_written,
                    components_written=result.component_sections_written,
                    pages_skipped=result.pages_skipped,
                    pages_errors=result.pages_errored,
                    schema_upgraded=result.pages_schema_upgraded,
                    has_more=result.has_more,
                )

    state.save()

    all_screen_paths = [p for r in all_results for p in r.md_paths]
    all_comp_paths = [p for r in all_results for p in r.component_paths]
    total_written = sum(r.pages_written for r in all_results)
    total_schema_upgraded = sum(r.pages_schema_upgraded for r in all_results)
    if all_screen_paths or all_comp_paths:
        parts = []
        if total_written:
            parts.append(f"{total_written} page(s)")
        if all_comp_paths:
            parts.append(f"{len(all_comp_paths)} component(s)")
        if total_schema_upgraded and not total_written:
            # Schema-only run: use a different verb so it's recognizable in git log
            parts.append(f"{total_schema_upgraded} page(s) schema-upgraded")
        click.echo(f"{COMMIT_MSG_PREFIX}sync: figmaclaw pull — {', '.join(parts)} updated")

    if has_more_global:
        click.echo(HAS_MORE_TRUE)

    obs.run_end(has_more_global=has_more_global)
