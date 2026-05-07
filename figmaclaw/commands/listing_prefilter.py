"""Shared Figma team file listing prefilter for CI commands."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime

import click

from figmaclaw.figma_api_models import FileSummary, ProjectSummary
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.figma_utils import parse_since
from figmaclaw.source_context import classify_source_lifecycle


@dataclass(frozen=True)
class ListingPrefilter:
    """Result of a team file listing pass."""

    last_modified_by_key: dict[str, str]
    tracked_before: int
    tracked_after: int
    newly_tracked: int


async def listing_prefilter(
    client: FigmaClient,
    team_id: str,
    state: FigmaSyncState,
    since: str,
    *,
    track_new: bool = True,
) -> ListingPrefilter:
    """List team files in parallel and update manifest source context.

    The returned ``last_modified_by_key`` map is a cheap freshness witness:
    when a tracked file's listed ``last_modified`` equals the manifest value,
    commands may skip more expensive file-scoped REST/MCP reads if their own
    registry state is already current for the manifest version.
    """

    since_dt: datetime | None = None
    if since:
        with contextlib.suppress(ValueError):
            since_dt = parse_since(since)

    projects = await client.list_team_projects(team_id)

    async def _list_project(project: ProjectSummary) -> tuple[ProjectSummary, list[FileSummary]]:
        try:
            return project, await client.list_project_files(str(project.id))
        except Exception:
            return project, []

    all_project_file_lists = await asyncio.gather(*[_list_project(p) for p in projects])

    listing_last_modified: dict[str, str] = {}
    newly_tracked = 0
    tracked_before = len(state.manifest.tracked_files)
    tracked = set(state.manifest.tracked_files)

    for project, files in all_project_file_lists:
        project_id = str(project.id)
        project_name = project.name
        for file_info in files:
            file_key = file_info.key
            file_name = file_info.name
            last_modified = file_info.last_modified
            if not file_key:
                continue

            if since_dt and last_modified and file_key not in tracked:
                try:
                    modified_dt = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
                    if modified_dt < since_dt:
                        continue
                except ValueError:
                    pass

            listing_last_modified[file_key] = last_modified

            if (
                track_new
                and file_key not in tracked
                and file_key not in state.manifest.skipped_files
            ):
                state.add_tracked_file(file_key, file_name)
                click.echo(f"  → now tracking {file_name!r}")
                tracked.add(file_key)
                newly_tracked += 1
            entry = state.manifest.files.get(file_key)
            if entry is not None:
                entry.source_project_id = project_id
                entry.source_project_name = project_name
                entry.source_lifecycle = classify_source_lifecycle(file_name, project_name)

    if newly_tracked:
        click.echo(f"NEWLY_TRACKED:{newly_tracked}")

    return ListingPrefilter(
        last_modified_by_key=listing_last_modified,
        tracked_before=tracked_before,
        tracked_after=len(state.manifest.tracked_files),
        newly_tracked=newly_tracked,
    )


def unchanged_in_listing(
    *,
    key: str,
    stored_last_modified: str,
    listing_last_modified: dict[str, str] | None,
) -> bool:
    """Return true when the team listing proves the tracked file is unchanged."""

    if listing_last_modified is None:
        return False
    listed_last_modified = listing_last_modified.get(key)
    return bool(listed_last_modified and stored_last_modified == listed_last_modified)
