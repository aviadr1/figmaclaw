"""Inspect one Figma instance against its master component."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import httpx

from figmaclaw.commands._shared import require_figma_api_key
from figmaclaw.config import load_config
from figmaclaw.figma_client import FigmaClient
from figmaclaw.instance_diff import InstanceDiff, diff_instance_against_master


@click.command("inspect-instance")
@click.option("--file-key", required=True, help="Figma file key containing the instance.")
@click.option("--node", "node_id", required=True, help="INSTANCE node id to inspect.")
@click.option(
    "--current-ds-hash",
    "current_ds_hashes",
    multiple=True,
    help="Current design-system library hash. Repeatable.",
)
@click.pass_context
def inspect_instance_cmd(
    ctx: click.Context,
    file_key: str,
    node_id: str,
    current_ds_hashes: tuple[str, ...],
) -> None:
    """Print an instance/master property diff as JSON."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    config = load_config(repo_dir)
    current_hashes = {
        *config.design_system_library_hashes,
        *(value.strip() for value in current_ds_hashes if value.strip()),
    }
    current_file_keys = set(config.design_system_file_keys)
    current_published_keys = {*config.design_system_published_keys, *current_hashes}
    try:
        result = asyncio.run(
            _run(
                api_key,
                file_key,
                node_id,
                current_hashes,
                current_file_keys,
                current_published_keys,
            )
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(f"Figma API request failed: {exc}") from exc
    click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))


async def _run(
    api_key: str,
    file_key: str,
    node_id: str,
    current_ds_hashes: set[str],
    current_ds_file_keys: set[str],
    current_ds_published_keys: set[str],
) -> InstanceDiff:
    async with FigmaClient(api_key) as client:
        return await diff_instance_against_master(
            client,
            file_key,
            node_id,
            current_ds_library_hashes=current_ds_hashes,
            current_ds_file_keys=current_ds_file_keys,
            current_ds_published_keys=current_ds_published_keys,
        )
