"""figmaclaw variables — refresh the file-scope design-token catalog."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from figmaclaw.commands._shared import (
    figma_variables_api_key,
    load_state,
    require_figma_api_key,
    require_tracked_files,
)
from figmaclaw.figma_api_models import LocalVariablesResponse
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_mcp import FigmaMcpError
from figmaclaw.figma_variables_mcp import get_local_variables_via_mcp
from figmaclaw.git_utils import git_commit
from figmaclaw.status_markers import COMMIT_MSG_PREFIX
from figmaclaw.token_catalog import (
    AUTHORITATIVE_DEFINITION_SOURCES,
    catalog_path,
    libraries_for_file,
    load_catalog,
    mark_local_variables_unavailable,
    merge_local_variables,
    save_catalog,
)


@click.command("variables")
@click.option(
    "--file-key",
    "file_key",
    default=None,
    help="Refresh variables only for this file key (default: all tracked files).",
)
@click.option(
    "--auto-commit", "auto_commit", is_flag=True, help="git commit written ds_catalog.json."
)
@click.option("--force", is_flag=True, help="Refresh even if source_version is current.")
@click.option(
    "--source",
    "source",
    type=click.Choice(["auto", "rest", "mcp"]),
    default="auto",
    show_default=True,
    help="Variable-definition reader to use.",
)
@click.pass_context
def variables_cmd(
    ctx: click.Context,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
    source: str,
) -> None:
    """Refresh .figma-sync/ds_catalog.json from Figma local variables."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    asyncio.run(_run(api_key, repo_dir, file_key, auto_commit, force, source))


async def _run(
    api_key: str,
    repo_dir: Path,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
    source: str,
) -> None:
    state = load_state(repo_dir)
    if not require_tracked_files(state):
        return

    keys = [file_key] if file_key else list(state.manifest.tracked_files)
    written = False

    async with FigmaClient(
        api_key,
        variables_api_key=figma_variables_api_key(api_key),
    ) as client:
        catalog = load_catalog(repo_dir)

        for key in keys:
            if key not in state.manifest.tracked_files:
                click.echo(f"{key}: not tracked — skip")
                continue

            skip_reason = state.manifest.skipped_files.get(key)
            if skip_reason:
                click.echo(f"{key}: skipped — {skip_reason}")
                continue

            try:
                meta = await client.get_file_meta(key)
            except Exception as exc:
                click.echo(f"{key}: failed to fetch file meta — {exc}")
                continue

            current_libraries = libraries_for_file(catalog, key)
            if (
                not force
                and current_libraries
                and all(
                    lib.source_version == meta.version
                    and lib.source in AUTHORITATIVE_DEFINITION_SOURCES
                    for lib in current_libraries
                )
            ):
                click.echo(f"{meta.name}: variables unchanged (version {meta.version})")
                continue

            before = _catalog_text(repo_dir)
            try:
                response, response_source = await _get_local_variables(client, key, source)
            except Exception as exc:
                if source == "mcp":
                    raise click.ClickException(
                        f"{key} ({meta.name}): MCP variables export failed — {exc}"
                    ) from exc
                click.echo(f"{key} ({meta.name}): failed — {exc}")
                continue

            if response is None:
                mark_local_variables_unavailable(
                    catalog,
                    file_key=key,
                    file_name=meta.name,
                    file_version=meta.version,
                )
                click.echo(
                    f"{meta.name}: variables definitions unavailable; "
                    "kept seeded catalog fallback current"
                )
            else:
                count = merge_local_variables(
                    catalog,
                    response,
                    file_key=key,
                    file_name=meta.name,
                    file_version=meta.version,
                    source=response_source,
                )
                click.echo(f"{meta.name}: refreshed {count} variable(s) via {response_source}")

            save_catalog(catalog, repo_dir)
            after = _catalog_text(repo_dir)
            if before != after:
                written = True
                if auto_commit:
                    rel = ".figma-sync/ds_catalog.json"
                    committed = git_commit(
                        repo_dir, [rel], f"sync: figmaclaw variables — {meta.name}"
                    )
                    if committed:
                        click.echo("  ✓ committed")

    if written:
        click.echo(f"{COMMIT_MSG_PREFIX}sync: figmaclaw variables updated")


def _catalog_text(repo_dir: Path) -> str | None:
    path = catalog_path(repo_dir)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


async def _get_local_variables(
    client: FigmaClient,
    file_key: str,
    source: str,
) -> tuple[LocalVariablesResponse | None, str]:
    if source in {"auto", "rest"}:
        response = await client.get_local_variables(file_key)
        if response is not None or source == "rest":
            return response, "figma_api"
        click.echo(
            f"{file_key}: REST variables endpoint unavailable (403); trying Figma MCP fallback"
        )

    try:
        return await get_local_variables_via_mcp(file_key), "figma_mcp"
    except FigmaMcpError as exc:
        if source == "mcp":
            raise
        click.echo(f"{file_key}: Figma MCP variables fallback unavailable — {exc}")
        return None, "figma_mcp"
