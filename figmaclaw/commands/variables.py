"""figmaclaw variables — refresh the file-scope design-token catalog."""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import click

from figmaclaw.commands._shared import (
    figma_variables_api_key,
    load_state,
    require_figma_api_key,
    require_tracked_files,
)
from figmaclaw.config import load_config
from figmaclaw.figma_api_models import LocalVariablesResponse
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_mcp import FigmaMcpError
from figmaclaw.figma_variables_mcp import get_local_variables_via_mcp
from figmaclaw.git_utils import git_commit
from figmaclaw.source_context import SourceContext, source_context_from_manifest_entry
from figmaclaw.status_markers import COMMIT_MSG_PREFIX
from figmaclaw.token_catalog import (
    AUTHORITATIVE_DEFINITION_SOURCES,
    CatalogLibrary,
    TokenCatalog,
    catalog_path,
    has_figma_api_definitions_for_file,
    libraries_for_file,
    load_catalog,
    mark_local_variables_unavailable,
    merge_local_variables,
    save_catalog,
)

_UNAVAILABLE_RETRY_BACKOFF = datetime.timedelta(hours=24)


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
@click.option(
    "--require-authoritative",
    is_flag=True,
    help="Exit non-zero unless selected files have authoritative variable definitions.",
)
@click.pass_context
def variables_cmd(
    ctx: click.Context,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
    source: str,
    require_authoritative: bool,
) -> None:
    """Refresh .figma-sync/ds_catalog.json from Figma local variables."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    asyncio.run(
        _run(api_key, repo_dir, file_key, auto_commit, force, source, require_authoritative)
    )


async def _run(
    api_key: str,
    repo_dir: Path,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
    source: str,
    require_authoritative: bool,
) -> None:
    state = load_state(repo_dir)
    if not require_tracked_files(state):
        return
    config = load_config(repo_dir)
    rest_variables_enabled = config.is_enterprise()
    if source == "rest" and not rest_variables_enabled:
        raise click.ClickException(
            "REST variables require Figma Enterprise and file_variables:read. "
            'Set [tool.figmaclaw] license_type = "enterprise" to enable --source rest.'
        )

    keys = [file_key] if file_key else list(state.manifest.tracked_files)
    written = False
    current_versions: dict[str, str] = {}

    async with FigmaClient(
        api_key,
        variables_api_key=figma_variables_api_key(api_key),
    ) as client:
        catalog = load_catalog(repo_dir)
        mcp_unavailable_reason: str | None = None
        rest_unavailable_reason: str | None = None

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
            current_versions[key] = meta.version
            editor_type = getattr(meta, "editorType", "") or ""
            source_context = source_context_from_manifest_entry(state.manifest.files.get(key))

            current_libraries = libraries_for_file(catalog, key)
            before_source_context_update = _catalog_text(repo_dir)
            if _apply_source_context_to_libraries(current_libraries, source_context):
                save_catalog(catalog, repo_dir)
                after_source_context_update = _catalog_text(repo_dir)
                if before_source_context_update != after_source_context_update:
                    written = True
                    if auto_commit:
                        rel = ".figma-sync/ds_catalog.json"
                        committed = git_commit(
                            repo_dir, [rel], f"sync: figmaclaw variables — {meta.name}"
                        )
                        if committed:
                            click.echo("  ✓ committed")
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
            if (
                not force
                and source == "auto"
                and current_libraries
                and _unavailable_retry_pending(current_libraries, meta.version)
            ):
                retry_after = _latest_retry_after(current_libraries)
                click.echo(
                    f"{meta.name}: variables unavailable unchanged (version {meta.version}); "
                    f"will retry after {retry_after}; use --force or --source mcp/rest to retry now"
                )
                continue
            if _mcp_variables_unsupported_for_editor_type(
                editor_type,
                source=source,
            ):
                before = _catalog_text(repo_dir)
                mark_local_variables_unavailable(
                    catalog,
                    file_key=key,
                    file_name=meta.name,
                    file_version=meta.version,
                    source_project_id=source_context.project_id,
                    source_project_name=source_context.project_name,
                    source_lifecycle=source_context.lifecycle,
                    unavailable_retry_after=_next_unavailable_retry_after()
                    if source == "auto"
                    else None,
                )
                click.echo(
                    f"{meta.name}: variables registry unavailable for editorType={editor_type!r}; "
                    "kept unavailable catalog marker current"
                )
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
                continue

            before = _catalog_text(repo_dir)
            try:
                (
                    response,
                    response_source,
                    mcp_unavailable_reason,
                    rest_unavailable_reason,
                ) = await _get_local_variables(
                    client,
                    key,
                    source,
                    mcp_unavailable_reason=mcp_unavailable_reason,
                    rest_unavailable_reason=rest_unavailable_reason,
                    rest_variables_enabled=rest_variables_enabled,
                )
            except Exception as exc:
                if source == "mcp":
                    raise click.ClickException(
                        f"{key} ({meta.name}): MCP variables export failed — {exc}"
                    ) from exc
                click.echo(f"{key} ({meta.name}): failed — {exc}")
                continue

            if response is None:
                authoritative_libraries = [
                    lib
                    for lib in current_libraries
                    if lib.source in AUTHORITATIVE_DEFINITION_SOURCES
                ]
                mark_local_variables_unavailable(
                    catalog,
                    file_key=key,
                    file_name=meta.name,
                    file_version=meta.version,
                    source_project_id=source_context.project_id,
                    source_project_name=source_context.project_name,
                    source_lifecycle=source_context.lifecycle,
                    unavailable_retry_after=_next_unavailable_retry_after()
                    if source == "auto"
                    else None,
                )
                if authoritative_libraries:
                    versions = ", ".join(
                        sorted({lib.source_version or "missing" for lib in authoritative_libraries})
                    )
                    click.echo(
                        f"{meta.name}: variables definitions unavailable; "
                        f"preserved authoritative catalog from version(s): {versions}"
                    )
                else:
                    click.echo(
                        f"{meta.name}: variables definitions unavailable; "
                        "kept unavailable catalog marker current"
                    )
            else:
                count = merge_local_variables(
                    catalog,
                    response,
                    file_key=key,
                    file_name=meta.name,
                    file_version=meta.version,
                    source=response_source,
                    source_project_id=source_context.project_id,
                    source_project_name=source_context.project_name,
                    source_lifecycle=source_context.lifecycle,
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

        if require_authoritative:
            # Canon AUTH-1: callers that claim authoritative token coverage must
            # fail on unavailable/observed-only/zero-definition catalogs instead
            # of letting downstream automation treat bridge data as DS truth.
            required_keys = [
                key
                for key in keys
                if key in state.manifest.tracked_files and key not in state.manifest.skipped_files
            ]
            errors = _authoritative_catalog_errors(catalog, required_keys, current_versions)
            if errors:
                raise click.ClickException(
                    "authoritative variables missing:\n"
                    + "\n".join(f"- {error}" for error in errors)
                    + "\nConfigure FIGMA_VARIABLES_TOKEN with file_variables:read "
                    "or FIGMA_MCP_TOKEN before relying on design-token definitions."
                )

    if written:
        click.echo(f"{COMMIT_MSG_PREFIX}sync: figmaclaw variables updated")


def _catalog_text(repo_dir: Path) -> str | None:
    path = catalog_path(repo_dir)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _apply_source_context_to_libraries(
    libraries: list[CatalogLibrary],
    source_context: SourceContext,
) -> bool:
    changed = False
    for library in libraries:
        if library.source_project_id != source_context.project_id:
            library.source_project_id = source_context.project_id
            changed = True
        if library.source_project_name != source_context.project_name:
            library.source_project_name = source_context.project_name
            changed = True
        if library.source_lifecycle != source_context.lifecycle:
            library.source_lifecycle = source_context.lifecycle
            changed = True
    return changed


def _next_unavailable_retry_after() -> str:
    return _format_utc(datetime.datetime.now(datetime.UTC) + _UNAVAILABLE_RETRY_BACKOFF)


def _format_utc(value: datetime.datetime) -> str:
    return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            datetime.UTC
        )
    except ValueError:
        return None


def _latest_retry_after(libraries: list[CatalogLibrary]) -> str:
    retry_after_values = sorted(
        value for lib in libraries if (value := lib.unavailable_retry_after)
    )
    return retry_after_values[-1] if retry_after_values else "unknown"


def _unavailable_retry_pending(libraries: list[CatalogLibrary], current_version: str) -> bool:
    if not all(
        lib.source == "unavailable" and lib.source_version == current_version for lib in libraries
    ):
        return False

    retry_after_values = [_parse_utc(lib.unavailable_retry_after) for lib in libraries]
    if not retry_after_values or any(value is None for value in retry_after_values):
        return False

    return max(value for value in retry_after_values if value is not None) > datetime.datetime.now(
        datetime.UTC
    )


def _mcp_variables_unsupported_for_editor_type(
    editor_type: str,
    *,
    source: str,
) -> bool:
    """Avoid using MCP write-tool export for non-Design files.

    Figma's read-only variable/context MCP tools are documented for Figma
    Design files. FigJam still supports ``use_figma`` for board inspection and
    edits, but it is not a reliable local-variable registry reader.
    """
    if editor_type.lower() != "figjam":
        return False
    if source == "mcp":
        raise click.ClickException(
            "MCP local variables export is unsupported for FigJam files; "
            "use a Figma Design file for authoritative variable definitions."
        )
    return source == "auto"


def _authoritative_catalog_errors(
    catalog: TokenCatalog,
    file_keys: list[str],
    current_versions: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    for key in file_keys:
        libraries = libraries_for_file(catalog, key)
        if not libraries:
            errors.append(f"{key}: no variables registry entry exists")
            continue

        sources = sorted({lib.source or "missing" for lib in libraries})
        if not any(source in AUTHORITATIVE_DEFINITION_SOURCES for source in sources):
            errors.append(
                f"{key}: variables registry is not authoritative "
                f"(library source(s): {', '.join(sources)})"
            )
            continue

        current_version = current_versions.get(key)
        if current_version and not any(
            lib.source in AUTHORITATIVE_DEFINITION_SOURCES and lib.source_version == current_version
            for lib in libraries
        ):
            versions = ", ".join(sorted({lib.source_version or "missing" for lib in libraries}))
            errors.append(
                f"{key}: authoritative variables registry is stale "
                f"(current version: {current_version}; catalog source_version(s): {versions})"
            )
            continue

        if not has_figma_api_definitions_for_file(catalog, key):
            errors.append(f"{key}: authoritative reader returned zero variable definitions")
    return errors


async def _get_local_variables(
    client: FigmaClient,
    file_key: str,
    source: str,
    *,
    mcp_unavailable_reason: str | None = None,
    rest_unavailable_reason: str | None = None,
    rest_variables_enabled: bool = False,
) -> tuple[LocalVariablesResponse | None, str, str | None, str | None]:
    # Canon ERR-1: only persistent configuration absence is cached across files.
    # Transient MCP/API failures must mark the current file unavailable but leave
    # later files free to retry the fallback reader.
    if source in {"auto", "rest"}:
        if source == "auto" and not rest_variables_enabled:
            rest_unavailable_reason = (
                "non-enterprise license; REST variables require file_variables:read"
            )
        elif source == "rest" and not rest_variables_enabled:
            return None, "figma_api", mcp_unavailable_reason, rest_unavailable_reason
        if not (source == "auto" and rest_unavailable_reason is not None):
            response, rest_reason = await _get_rest_local_variables_with_reason(client, file_key)
            if response is not None or source == "rest":
                if _is_persistent_rest_variables_unavailable(rest_reason):
                    rest_unavailable_reason = rest_reason
                return response, "figma_api", mcp_unavailable_reason, rest_unavailable_reason
            if _is_persistent_rest_variables_unavailable(rest_reason):
                click.echo(
                    f"{file_key}: REST variables endpoint unavailable for this run — "
                    "token lacks file_variables:read; trying Figma MCP fallback"
                )
                rest_unavailable_reason = rest_reason
            else:
                click.echo(
                    f"{file_key}: REST variables endpoint unavailable (403); trying Figma MCP fallback"
                )
        if mcp_unavailable_reason is not None:
            return None, "figma_mcp", mcp_unavailable_reason, rest_unavailable_reason

    try:
        return (
            await get_local_variables_via_mcp(file_key),
            "figma_mcp",
            mcp_unavailable_reason,
            rest_unavailable_reason,
        )
    except FigmaMcpError as exc:
        if source == "mcp":
            raise
        click.echo(f"{file_key}: Figma MCP variables fallback unavailable — {exc}")
        reason = str(exc)
        if _is_persistent_mcp_unavailable(reason):
            return None, "figma_mcp", reason, rest_unavailable_reason
        return None, "figma_mcp", mcp_unavailable_reason, rest_unavailable_reason


async def _get_rest_local_variables_with_reason(
    client: FigmaClient, file_key: str
) -> tuple[LocalVariablesResponse | None, str | None]:
    method = getattr(client, "get_local_variables_with_reason", None)
    if method is not None:
        response, reason = await method(file_key)
        return response, reason
    return await client.get_local_variables(file_key), None


def _is_persistent_mcp_unavailable(reason: str) -> bool:
    """True for configuration failures that will repeat for every file."""
    normalized = reason.lower()
    return any(
        marker in normalized
        for marker in (
            "figma_mcp_token",
            "credentials file not found",
            "no figma mcp token",
            "no figma token",
        )
    )


def _is_persistent_rest_variables_unavailable(reason: str | None) -> bool:
    """True when Figma says the variables token lacks the required scope."""
    if reason is None:
        return False
    normalized = reason.lower()
    return "invalid scope" in normalized and "file_variables:read" in normalized
