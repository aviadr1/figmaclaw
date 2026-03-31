"""figmaclaw pull — incremental sync of all tracked Figma files."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.figma_utils import make_anthropic_client
from figmaclaw.pull_logic import PullResult, pull_file


@click.command("pull")
@click.option("--file-key", "file_key", default=None, help="Pull only this file key.")
@click.option("--force", is_flag=True, help="Regenerate all pages even if hash is unchanged.")
@click.option("--no-llm", is_flag=True, help="Skip LLM description generation.")
@click.pass_context
def pull_cmd(ctx: click.Context, file_key: str | None, force: bool, no_llm: bool) -> None:
    """Pull all tracked Figma files and write changed pages to disk."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.UsageError("FIGMA_API_KEY environment variable is not set.")

    asyncio.run(_run(api_key, repo_dir, file_key, force, no_llm))


async def _run(api_key: str, repo_dir: Path, file_key: str | None, force: bool, no_llm: bool) -> None:
    state = FigmaSyncState(repo_dir)
    state.load()

    if not state.manifest.tracked_files:
        click.echo("No tracked files. Run 'figmaclaw track <file-key>' first.")
        return

    keys = [file_key] if file_key else state.manifest.tracked_files
    all_results: list[PullResult] = []

    anthropic_client = make_anthropic_client() if not no_llm else None
    if anthropic_client is None and not no_llm:
        click.echo("Note: ANTHROPIC_API_KEY not set — skipping LLM description generation.")

    async with FigmaClient(api_key) as client:
        for key in keys:
            if key not in state.manifest.tracked_files:
                click.echo(f"File key {key!r} is not tracked. Run 'figmaclaw track {key}' first.")
                continue
            result = await pull_file(client, key, state, repo_dir, force=force, anthropic_client=anthropic_client)
            all_results.append(result)
            if result.skipped_file:
                click.echo(f"{key}: unchanged (skipped)")
            else:
                click.echo(f"{key}: wrote {result.pages_written} page(s), {result.component_sections_written} component(s), skipped {result.pages_skipped}")
                for path in result.md_paths:
                    click.echo(f"  → {path}")
                for path in result.component_paths:
                    click.echo(f"  ❖ {path}")

    state.save()

    # Emit commit message for CI (read by GitHub Actions shell)
    all_screen_paths = [p for r in all_results for p in r.md_paths]
    all_comp_paths = [p for r in all_results for p in r.component_paths]
    if all_screen_paths or all_comp_paths:
        parts = []
        if all_screen_paths:
            parts.append(f"{len(all_screen_paths)} page(s)")
        if all_comp_paths:
            parts.append(f"{len(all_comp_paths)} component(s)")
        click.echo(f"COMMIT_MSG:sync: figmaclaw pull — {', '.join(parts)} updated")


