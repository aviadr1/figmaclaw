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
    help="Only show files modified within this window (e.g. '3m', '7d', '1y').",
)
@click.option("--track", is_flag=True, help="Track all listed files (run initial pull).")
@click.pass_context
def list_cmd(ctx: click.Context, team_id_or_url: str, since: str | None, track: bool) -> None:
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

    asyncio.run(_run(api_key, repo_dir, team_id, since_dt, track))


async def _run(
    api_key: str,
    repo_dir: Path,
    team_id: str,
    since_dt: datetime | None,
    track: bool,
) -> None:
    from figmaclaw.figma_utils import make_anthropic_client
    from figmaclaw.pull_logic import pull_file

    state = FigmaSyncState(repo_dir)
    state.load()
    tracked = set(state.manifest.tracked_files)

    async with FigmaClient(api_key) as client:
        projects = await client.list_team_projects(team_id)
        shown = 0

        for project in projects:
            project_name: str = project.get("name", "")
            project_id: str = str(project.get("id", ""))
            files = await client.list_project_files(project_id)

            for file_info in files:
                file_key: str = file_info.get("key", "")
                file_name: str = file_info.get("name", "")
                last_modified: str = file_info.get("last_modified", "")

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

                if track and file_key not in tracked:
                    meta = await client.get_file_meta(file_key)
                    file_name_full: str = meta.get("name", file_key)
                    state.add_tracked_file(file_key, file_name_full)
                    state.save()
                    click.echo(f"  → now tracking {file_name_full!r}")
                    anthropic_client = make_anthropic_client()
                    result = await pull_file(
                        client, file_key, state, repo_dir, force=True, anthropic_client=anthropic_client
                    )
                    state.save()
                    click.echo(f"  → wrote {result.pages_written} page(s)")
                    tracked.add(file_key)

    if shown == 0:
        click.echo("No files found matching the criteria.")
