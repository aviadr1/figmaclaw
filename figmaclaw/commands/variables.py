"""figmaclaw variables — refresh the file-scope design-token catalog."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from figmaclaw.commands._shared import load_state, require_figma_api_key, require_tracked_files
from figmaclaw.figma_client import FigmaClient
from figmaclaw.git_utils import git_commit
from figmaclaw.status_markers import COMMIT_MSG_PREFIX
from figmaclaw.token_catalog import (
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
@click.pass_context
def variables_cmd(
    ctx: click.Context,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
) -> None:
    """Refresh .figma-sync/ds_catalog.json from Figma local variables."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    asyncio.run(_run(api_key, repo_dir, file_key, auto_commit, force))


async def _run(
    api_key: str,
    repo_dir: Path,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
) -> None:
    state = load_state(repo_dir)
    if not require_tracked_files(state):
        return

    keys = [file_key] if file_key else list(state.manifest.tracked_files)
    written = False

    async with FigmaClient(api_key) as client:
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
                and all(lib.source_version == meta.version for lib in current_libraries)
            ):
                click.echo(f"{meta.name}: variables unchanged (version {meta.version})")
                continue

            before = _catalog_text(repo_dir)
            try:
                response = await client.get_local_variables(key)
            except Exception as exc:
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
                    f"{meta.name}: variables endpoint unavailable (403); "
                    "kept seeded catalog fallback current"
                )
            else:
                count = merge_local_variables(
                    catalog,
                    response,
                    file_key=key,
                    file_name=meta.name,
                    file_version=meta.version,
                )
                click.echo(f"{meta.name}: refreshed {count} variable(s)")

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
