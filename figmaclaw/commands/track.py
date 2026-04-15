"""figmaclaw track — register a Figma file for syncing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from figmaclaw.commands._shared import load_state, require_figma_api_key
from figmaclaw.figma_client import FigmaClient
from figmaclaw.pull_logic import pull_file


@click.command("track")
@click.argument("file_key")
@click.option("--no-pull", is_flag=True, help="Register the file without running an initial pull.")
@click.pass_context
def track_cmd(ctx: click.Context, file_key: str, no_pull: bool) -> None:
    """Register a Figma file for syncing and run an initial pull."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()

    asyncio.run(_run(api_key, repo_dir, file_key, no_pull))


async def _run(api_key: str, repo_dir: Path, file_key: str, no_pull: bool) -> None:
    state = load_state(repo_dir)

    async with FigmaClient(api_key) as client:
        # Validate the file exists and get its name
        meta = await client.get_file_meta(file_key)
        file_name = meta.name

        state.add_tracked_file(file_key, file_name)
        state.save()
        click.echo(f"Tracking {file_name!r} ({file_key})")

        if not no_pull:
            click.echo("Running initial pull...")
            result = await pull_file(client, file_key, state, repo_dir, force=True)
            click.echo(f"Wrote {result.pages_written} page(s)")
            for path in result.md_paths:
                click.echo(f"  → {path}")
            state.save()
