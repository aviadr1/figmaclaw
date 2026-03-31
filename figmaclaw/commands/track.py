"""figmaclaw track — register a Figma file for syncing."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_utils import make_anthropic_client
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.pull_logic import pull_file


@click.command("track")
@click.argument("file_key")
@click.option("--no-pull", is_flag=True, help="Register the file without running an initial pull.")
@click.pass_context
def track_cmd(ctx: click.Context, file_key: str, no_pull: bool) -> None:
    """Register a Figma file for syncing and run an initial pull."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.UsageError("FIGMA_API_KEY environment variable is not set.")

    asyncio.run(_run(api_key, repo_dir, file_key, no_pull))


async def _run(api_key: str, repo_dir: Path, file_key: str, no_pull: bool) -> None:
    state = FigmaSyncState(repo_dir)
    state.load()

    async with FigmaClient(api_key) as client:
        # Validate the file exists and get its name
        meta = await client.get_file_meta(file_key)
        file_name = meta.get("name", file_key)

        state.add_tracked_file(file_key, file_name)
        state.save()
        click.echo(f"Tracking {file_name!r} ({file_key})")

        if not no_pull:
            click.echo("Running initial pull...")
            anthropic_client = make_anthropic_client()
            result = await pull_file(client, file_key, state, repo_dir, force=True, anthropic_client=anthropic_client)
            click.echo(f"Wrote {result.pages_written} page(s)")
            for path in result.md_paths:
                click.echo(f"  → {path}")
            state.save()
