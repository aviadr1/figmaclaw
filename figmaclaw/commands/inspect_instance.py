"""Inspect one Figma instance against its master component."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import httpx

from figmaclaw.commands._shared import require_figma_api_key
from figmaclaw.config import load_config
from figmaclaw.figma_client import FigmaClient, normalize_node_id
from figmaclaw.instance_diff import (
    InstanceDiff,
    InstanceDiffError,
    diff_instances_against_masters,
)


@click.command("inspect-instance")
@click.option("--file-key", required=True, help="Figma file key containing the instance.")
@click.option("--node", "node_ids", multiple=True, help="INSTANCE node id to inspect. Repeatable.")
@click.option(
    "--nodes-from",
    "nodes_from",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Read INSTANCE node ids from a fetch-nodes JSONL file.",
)
@click.option(
    "--filter",
    "record_filter",
    help="Filter records from --nodes-from. Currently supports type=INSTANCE.",
)
@click.option(
    "--current-ds-hash",
    "current_ds_hashes",
    multiple=True,
    help="Current design-system library/published key or file key. Repeatable.",
)
@click.pass_context
def inspect_instance_cmd(
    ctx: click.Context,
    file_key: str,
    node_ids: tuple[str, ...],
    nodes_from: Path | None,
    record_filter: str | None,
    current_ds_hashes: tuple[str, ...],
) -> None:
    """Print instance/master property diffs as JSON or JSONL."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    config = load_config(repo_dir)
    cli_identifiers = {value.strip() for value in current_ds_hashes if value.strip()}
    current_hashes = {
        *config.design_system_library_hashes,
        *cli_identifiers,
    }
    current_file_keys = {*config.design_system_file_keys, *cli_identifiers}
    current_published_keys = {*config.design_system_published_keys, *current_hashes}
    requested_node_ids = list(node_ids)
    if nodes_from is not None:
        requested_node_ids.extend(_node_ids_from_jsonl(nodes_from, record_filter=record_filter))
    requested_node_ids = list(
        dict.fromkeys(normalize_node_id(node_id) for node_id in requested_node_ids)
    )
    skipped_synthesized = [
        node_id for node_id in requested_node_ids if _is_synthesized_node_id(node_id)
    ]
    requested_node_ids = [
        node_id for node_id in requested_node_ids if not _is_synthesized_node_id(node_id)
    ]
    if not requested_node_ids:
        raise click.UsageError("Provide at least one resolvable --node or --nodes-from record.")
    if skipped_synthesized:
        click.echo(
            f"skipped {len(skipped_synthesized)} synthesized nested instance ids",
            err=True,
        )
    try:
        results = asyncio.run(
            _run(
                api_key,
                file_key,
                requested_node_ids,
                current_hashes,
                current_file_keys,
                current_published_keys,
            )
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(f"Figma API request failed: {exc}") from exc
    if len(results) == 1:
        click.echo(json.dumps(results[0].model_dump(mode="json"), indent=2, sort_keys=True))
        return
    for result in results:
        click.echo(json.dumps(result.model_dump(mode="json"), sort_keys=True))


async def _run(
    api_key: str,
    file_key: str,
    node_ids: list[str],
    current_ds_hashes: set[str],
    current_ds_file_keys: set[str],
    current_ds_published_keys: set[str],
) -> list[InstanceDiff | InstanceDiffError]:
    async with FigmaClient(api_key) as client:
        expanded_published_keys = set(current_ds_published_keys)
        expanded_published_keys.update(
            await _published_component_set_keys_for_files(client, current_ds_file_keys)
        )
        return await diff_instances_against_masters(
            client,
            file_key,
            node_ids,
            current_ds_library_hashes=current_ds_hashes,
            current_ds_file_keys=current_ds_file_keys,
            current_ds_published_keys=expanded_published_keys,
        )


def _node_ids_from_jsonl(path: Path, *, record_filter: str | None) -> list[str]:
    if record_filter and record_filter != "type=INSTANCE":
        raise click.UsageError("--filter currently only supports type=INSTANCE")
    node_ids: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise click.UsageError(f"{path}:{line_number}: invalid JSONL record") from exc
            if not isinstance(record, dict):
                continue
            if record_filter == "type=INSTANCE" and record.get("type") != "INSTANCE":
                continue
            node_id = record.get("id") or record.get("node_id") or record.get("nodeId")
            if isinstance(node_id, str) and node_id.strip():
                node_ids.append(node_id.strip())
    return node_ids


def _is_synthesized_node_id(node_id: str) -> bool:
    return ";" in node_id


async def _published_component_set_keys_for_files(
    client: FigmaClient,
    file_keys: set[str],
) -> set[str]:
    keys: set[str] = set()
    for file_key in sorted(file_keys):
        if not _looks_like_figma_file_key(file_key):
            continue
        try:
            component_sets = await client.get_component_sets(file_key)
        except httpx.HTTPStatusError:
            continue
        for component_set in component_sets:
            if isinstance(component_set, dict):
                key = component_set.get("key")
                if isinstance(key, str) and key.strip():
                    keys.add(key.strip())
    return keys


def _looks_like_figma_file_key(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if len(stripped) == 40 and all(char in "0123456789abcdefABCDEF" for char in stripped):
        return False
    return 15 <= len(stripped) <= 40 and stripped.isalnum()
