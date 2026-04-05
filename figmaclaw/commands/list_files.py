"""figmaclaw list — discover and list Figma files for a team."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.figma_utils import parse_since, parse_team_id_from_url


@click.command("list")
@click.argument("team_id_or_url")
@click.option(
    "--since",
    "since",
    default=None,
    help="Only show files modified within this window (e.g. '3m', '7d', '1y', 'all').",
)
@click.option("--track", is_flag=True, help="Track all listed files and run initial pull.")
@click.option("--track-only", "track_only", is_flag=True, help="Track all listed files without pulling (pull loop handles it).")
@click.pass_context
def list_cmd(ctx: click.Context, team_id_or_url: str, since: str | None, track: bool, track_only: bool) -> None:
    """List Figma files for a team, optionally filtered by last-modified date."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.UsageError("FIGMA_API_KEY environment variable is not set.")

    team_id = parse_team_id_from_url(team_id_or_url)
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = parse_since(since)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc

    asyncio.run(_run(api_key, repo_dir, team_id, since_dt, track, track_only))


async def _run(
    api_key: str,
    repo_dir: Path,
    team_id: str,
    since_dt: datetime | None,
    track: bool,
    track_only: bool,
) -> None:
    from figmaclaw.pull_logic import pull_file

    state = FigmaSyncState(repo_dir)
    state.load()
    tracked = set(state.manifest.tracked_files)

    async with FigmaClient(api_key) as client:
        projects = await client.list_team_projects(team_id)
        shown = 0
        newly_tracked = 0

        for project in projects:
            project_name = project.name
            project_id = str(project.id)
            files = await client.list_project_files(project_id)

            for file_info in files:
                file_key = file_info.key
                file_name = file_info.name
                last_modified = file_info.last_modified

                if since_dt and last_modified:
                    try:
                        modified_dt = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
                        if modified_dt < since_dt:
                            continue
                    except ValueError:
                        pass  # unparseable date — include it

                status = " [tracked]" if file_key in tracked else ""
                date_str = last_modified[:10] if last_modified else "unknown"
                click.echo(f"{file_key}  {file_name!r}  ({project_name})  {date_str}{status}")
                shown += 1

                if (track or track_only) and file_key not in tracked:
                    state.add_tracked_file(file_key, file_name)
                    state.save()
                    click.echo(f"  → now tracking {file_name!r}")
                    tracked.add(file_key)
                    newly_tracked += 1

                    if track:  # --track does immediate pull; --track-only defers to pull loop
                        try:
                            result = await pull_file(
                                client, file_key, state, repo_dir, force=True
                            )
                            state.save()
                            click.echo(f"  → wrote {result.pages_written} page(s)")
                        except Exception as exc:
                            click.echo(f"  → pull failed: {exc}")

        if newly_tracked:
            click.echo(f"NEWLY_TRACKED:{newly_tracked}")

    if shown == 0:
        click.echo("No files found matching the criteria.")
