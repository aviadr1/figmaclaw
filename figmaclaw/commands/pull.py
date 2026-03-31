"""figmaclaw pull — incremental sync of all tracked Figma files."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.figma_utils import make_anthropic_client, parse_since
from figmaclaw.pull_logic import PullResult, pull_file


@click.command("pull")
@click.option("--file-key", "file_key", default=None, help="Pull only this file key.")
@click.option("--force", is_flag=True, help="Regenerate all pages even if hash is unchanged.")
@click.option("--no-llm", is_flag=True, help="Skip LLM description generation.")
@click.option("--max-pages", "max_pages", default=None, type=int, help="Global page budget per run (batch loop mode).")
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit after each page. CI should do a final git push.")
@click.option("--push-every", "push_every", default=10, type=int, show_default=True, help="Push every N commits when --auto-commit is set.")
@click.option("--team-id", "team_id", default=None, envvar="FIGMA_TEAM_ID", help="Figma team ID. Enables fast listing pre-filter and auto-discovery of new files.")
@click.option("--since", "since", default="3m", show_default=True, help="When --team-id is set, only track files modified within this window (e.g. 3m, 7d, all).")
@click.pass_context
def pull_cmd(ctx: click.Context, file_key: str | None, force: bool, no_llm: bool, max_pages: int | None, auto_commit: bool, push_every: int, team_id: str | None, since: str) -> None:
    """Pull all tracked Figma files and write changed pages to disk."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.UsageError("FIGMA_API_KEY environment variable is not set.")

    asyncio.run(_run(api_key, repo_dir, file_key, force, no_llm, max_pages, auto_commit, push_every, team_id, since))


def _git_commit_page(repo_dir: Path, page_label: str) -> bool:
    """Stage figma/ and .figma-sync/, commit if anything changed. Returns True if committed."""
    subprocess.run(["git", "-C", str(repo_dir), "add", "figma/", ".figma-sync/"], check=False)
    diff = subprocess.run(["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        return False  # nothing staged
    msg = f"sync: figmaclaw — {page_label}"
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", msg], check=False)
    return True


def _git_push(repo_dir: Path) -> None:
    result = subprocess.run(["git", "-C", str(repo_dir), "push"], check=False)
    if result.returncode != 0:
        # Another push landed — pull and retry once
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--no-rebase"], check=False)
        subprocess.run(["git", "-C", str(repo_dir), "push"], check=False)


async def _listing_prefilter(
    client: FigmaClient,
    team_id: str,
    state: FigmaSyncState,
    since: str,
) -> dict[str, str]:
    """List all team files in parallel, track new ones, return {file_key: last_modified}.

    Files whose last_modified matches the stored value can be skipped without
    calling get_file_meta — the cheap listing replaces 63 individual meta calls
    in the no-op case.
    """
    from figmaclaw.figma_utils import parse_since
    from datetime import datetime

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = parse_since(since)
        except ValueError:
            pass

    projects = await client.list_team_projects(team_id)

    # Fetch all project file listings concurrently
    async def _list_project(project: dict) -> list[dict]:
        try:
            return await client.list_project_files(str(project.get("id", "")))
        except Exception:
            return []

    all_file_lists = await asyncio.gather(*[_list_project(p) for p in projects])

    listing_last_modified: dict[str, str] = {}
    newly_tracked = 0
    tracked = set(state.manifest.tracked_files)

    for files in all_file_lists:
        for file_info in files:
            file_key: str = file_info.get("key", "")
            file_name: str = file_info.get("name", "")
            last_modified: str = file_info.get("last_modified", "")
            if not file_key:
                continue

            # Date filter for auto-discovery only
            if since_dt and last_modified and file_key not in tracked:
                try:
                    modified_dt = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
                    if modified_dt < since_dt:
                        continue
                except ValueError:
                    pass

            listing_last_modified[file_key] = last_modified

            if file_key not in tracked:
                state.add_tracked_file(file_key, file_name)
                click.echo(f"  → now tracking {file_name!r}")
                tracked.add(file_key)
                newly_tracked += 1

    if newly_tracked:
        click.echo(f"NEWLY_TRACKED:{newly_tracked}")

    return listing_last_modified


async def _run(
    api_key: str,
    repo_dir: Path,
    file_key: str | None,
    force: bool,
    no_llm: bool,
    max_pages: int | None,
    auto_commit: bool,
    push_every: int,
    team_id: str | None,
    since: str,
) -> None:
    state = FigmaSyncState(repo_dir)
    state.load()

    anthropic_client = make_anthropic_client() if not no_llm else None
    if anthropic_client is None and not no_llm:
        click.echo("Note: ANTHROPIC_API_KEY not set — skipping LLM description generation.")

    commit_count = 0

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

    async with FigmaClient(api_key) as client:
        # Fast listing pre-filter: one listing pass replaces N individual get_file_meta
        # calls for unchanged files. Also handles auto-discovery when team_id is set.
        # None means "no listing available" (team_id not set); {} means "listing ran but
        # returned no files" — both are handled correctly by the is not None check below.
        listing_last_modified: dict[str, str] | None = None
        if team_id and not file_key:
            listing_last_modified = await _listing_prefilter(client, team_id, state, since)
            state.save()  # persist any newly tracked files before pulling

        if not state.manifest.tracked_files:
            click.echo("No tracked files. Run 'figmaclaw track <file-key>' first.")
            return

        keys = [file_key] if file_key else list(state.manifest.tracked_files)

        for key in keys:
            if key not in state.manifest.tracked_files:
                click.echo(f"File key {key!r} is not tracked. Run 'figmaclaw track {key}' first.")
                continue

            if max_pages is not None and pages_budget is not None and pages_budget <= 0:
                has_more_global = True
                break

            # Listing pre-filter: skip get_file_meta entirely when the listing tells us
            # the file hasn't changed (or isn't reachable at all).
            #   - listing_lm is None  → file not in team listing (e.g. FigJam boards);
            #     skip — if it's not reachable via the listing, it can't have changed
            #   - listing_lm == stored → last_modified unchanged; skip
            #   - listing_lm != stored → file changed; proceed to get_file_meta
            if not force and listing_last_modified is not None:
                listing_lm = listing_last_modified.get(key)
                stored_entry = state.manifest.files.get(key)
                stored_lm = stored_entry.last_modified if stored_entry else ""
                if listing_lm is None or stored_lm == listing_lm:
                    continue

            try:
                result = await pull_file(
                    client, key, state, repo_dir,
                    force=force,
                    anthropic_client=anthropic_client,
                    max_pages=pages_budget,
                    on_page_written=on_page_written,
                )
            except Exception as exc:
                click.echo(f"{key}: error — {exc} (skipping)")
                continue
            all_results.append(result)

            if max_pages is not None and pages_budget is not None:
                pages_budget -= result.pages_written
            if result.has_more:
                has_more_global = True

            if result.skipped_file:
                click.echo(f"{key}: unchanged (skipped)")
            else:
                errored = f", {result.pages_errored} error(s)" if result.pages_errored else ""
                click.echo(f"{key}: wrote {result.pages_written} page(s), {result.component_sections_written} component(s), skipped {result.pages_skipped}{errored}")
                for path in result.md_paths:
                    click.echo(f"  → {path}")
                for path in result.component_paths:
                    click.echo(f"  ❖ {path}")

    state.save()

    all_screen_paths = [p for r in all_results for p in r.md_paths]
    all_comp_paths = [p for r in all_results for p in r.component_paths]
    if all_screen_paths or all_comp_paths:
        parts = []
        if all_screen_paths:
            parts.append(f"{len(all_screen_paths)} page(s)")
        if all_comp_paths:
            parts.append(f"{len(all_comp_paths)} component(s)")
        click.echo(f"COMMIT_MSG:sync: figmaclaw pull — {', '.join(parts)} updated")

    if has_more_global:
        click.echo("HAS_MORE:true")
