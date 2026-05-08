"""figmaclaw apply-tokens — emit or execute token-binding write batches."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from figmaclaw.apply_tokens import (
    DEFAULT_NAMESPACE,
    DEFAULT_SIGNATURE_ABORT_THRESHOLD,
    EXIT_OPERATOR_ACTION_REQUIRED,
    apply_plan_report,
    load_apply_token_input,
    operator_action_for_signature,
    referenced_catalog_source_file_keys,
    refusal_report,
    write_apply_batches,
)
from figmaclaw.commands._shared import load_state
from figmaclaw.commands.reporting import (
    emit_json_value,
    resolve_output_path,
    resolve_repo_path,
    write_json_output,
)
from figmaclaw.token_catalog import (
    TokenCatalog,
    catalog_staleness_errors,
    load_catalog,
)
from figmaclaw.use_figma_batches import use_figma_batch_options
from figmaclaw.use_figma_exec import execute_use_figma_calls


@click.command("apply-tokens")
@click.argument("input_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--file", "file_key", required=True, help="Figma file key to apply against.")
@click.option("--page", "page_node_id", required=True, help="Target page node id.")
@click.option(
    "--catalog",
    "catalog_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional ds_catalog.json path. Defaults to .figma-sync/ds_catalog.json.",
)
@click.option(
    "--library",
    "libraries",
    multiple=True,
    help=(
        "Limit compact-row token resolution to libraries whose name or library_hash "
        "contains this substring. Repeatable."
    ),
)
@use_figma_batch_options(default_batch_size=100)
@click.option("--namespace", default=DEFAULT_NAMESPACE, show_default=True)
@click.option(
    "--remaining-out",
    "remaining_out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON path for all refused rows so operators can iterate upstream policy.",
)
@click.option(
    "--node-map",
    type=click.Choice(["shared-plugin-data", "direct"]),
    default="shared-plugin-data",
    show_default=True,
    help="Resolve source node ids through audit-page idMap, or apply node ids directly.",
)
@click.option(
    "--allow-stale-catalog",
    is_flag=True,
    help="Do not refuse when ds_catalog source_version is older than the manifest.",
)
@click.option(
    "--allow-non-authoritative",
    is_flag=True,
    help="Allow variables whose catalog source is not figma_api or figma_mcp.",
)
@click.option(
    "--allow-variable-id-fallback",
    is_flag=True,
    help="Allow emitted JS to fall back to getVariableByIdAsync when variable_key is unavailable.",
)
@click.option(
    "--legacy-bindings-for-figma",
    is_flag=True,
    help=(
        "Compatibility mode for migration-generated bindings_for_figma.json rows: "
        "allows variable-id fallback. Pair with --library to match the legacy target library."
    ),
)
@click.option(
    "--signature-abort-threshold",
    type=click.IntRange(min=1),
    default=None,
    show_default="5",
    help=(
        "F48: abort the phase after this many rows hit the same root-cause "
        "signature (e.g. unloadable_font:Boldonse Bold). Surfaces ONE F36 "
        "operator-actionable block instead of N identical lines in the "
        "report. Default 5; pass a larger value to tolerate more identical "
        "failures before aborting."
    ),
)
@click.pass_context
def apply_tokens_cmd(
    ctx: click.Context,
    input_path: Path,
    file_key: str,
    page_node_id: str,
    catalog_path: Path | None,
    libraries: tuple[str, ...],
    batch_dir: Path | None,
    batch_size: int,
    namespace: str,
    remaining_out_path: Path | None,
    node_map: str,
    mode: str,
    resume_from: int,
    continue_on_error: bool,
    allow_stale_catalog: bool,
    allow_non_authoritative: bool,
    allow_variable_id_fallback: bool,
    legacy_bindings_for_figma: bool,
    signature_abort_threshold: int | None,
    json_output: bool,
) -> None:
    """Apply authoritative, pre-filtered token fixes back into Figma.

    The command accepts a versioned apply-tokens manifest or legacy compact
    ``bindings_for_figma.json`` rows. Default mode is dry-run; use
    ``--emit-only`` for deterministic batch files or ``--execute`` to run them
    through the shared MCP executor.

    This command does not enforce migration policy or F16 inheritance
    preservation. Inputs must already be filtered by an upstream resolver such
    as the planned ``figmaclaw bindings prepare`` stage.

    Legacy ``bindings_for_figma.json`` rows only carry token names, so use
    ``--library`` when those names can exist in multiple catalog libraries.

    The accepted compact-row shape is::

        [
          {"n": "<source-node-id>", "p": "fill", "t": "fg/inverse"},
          {"node_id": "<id>", "property": "fontFamily",
           "token_name": "typography/family/sans"}
        ]

    Short (``n``/``p``/``t``/``v``) and long (``node_id``/``property``/
    ``token_name``/``value``) field names are interchangeable, and rows MAY
    mix them per-row. Keys outside of ``{n, p, t, v, node_id, property,
    token_name, value, variable_key, paint_index}`` are listed back as
    ``unrecognised_compact_row_fields`` in the refusal, alongside any
    canonical fields whose accepted aliases were absent
    (``missing_canonical_fields``), so authors know exactly what to rename.

    Token names with a leading ``<library>:`` prefix (e.g. ``tapin:fg/inverse``)
    are not in the catalog by that name; pass the bare name and use
    ``--library "TAP IN"`` to scope resolution. The refusal's
    ``did_you_mean_token_name`` field surfaces the stripped form when a
    prefix is detected.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    try:
        if catalog_path is not None:
            # load_catalog expects a repo root. Preserve that for the default path,
            # but allow direct catalog-path use for migration folders and tests.
            catalog = TokenCatalog.model_validate_json(
                resolve_repo_path(repo_dir, catalog_path).read_text(encoding="utf-8")
            )
        else:
            catalog = load_catalog(repo_dir)
    except (OSError, ValueError, ValidationError) as exc:
        raise click.UsageError(f"failed to load catalog: {exc}") from exc

    try:
        library_hashes = _resolve_library_filter(catalog, libraries)
        prepared = load_apply_token_input(
            resolve_repo_path(repo_dir, input_path),
            file_key=file_key,
            page_node_id=page_node_id,
            catalog=catalog,
            allow_non_authoritative=allow_non_authoritative,
            allow_variable_id_fallback=(allow_variable_id_fallback or legacy_bindings_for_figma),
            allow_catalog_source_mismatch=allow_stale_catalog,
            library_hashes=library_hashes,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc

    if not allow_stale_catalog:
        state = load_state(repo_dir)
        source_file_keys = referenced_catalog_source_file_keys(prepared, catalog)
        for source_file_key in sorted(source_file_keys):
            errors = catalog_staleness_errors(catalog, state, source_file_key)
            if errors:
                raise click.ClickException(errors[0])

    report = apply_plan_report(prepared)
    report["mode"] = mode
    if remaining_out_path is not None:
        write_json_output(repo_dir, remaining_out_path, refusal_report(prepared))

    if mode == "dry-run":
        if json_output or ctx.obj.get("json"):
            emit_json_value(report)
            return
        _emit_human_plan(report)
        return

    if not prepared.ok:
        emit_json_value(report)
        raise click.ClickException(
            f"refusing to emit apply batches: {report['refusals']} refused row(s)"
        )
    if batch_dir is None:
        raise click.UsageError("--batch-dir is required for --emit-only and --execute")
    if resume_from < 1:
        raise click.UsageError("--resume-from must be >= 1")

    threshold = (
        signature_abort_threshold
        if signature_abort_threshold is not None
        else DEFAULT_SIGNATURE_ABORT_THRESHOLD
    )
    batch_result = write_apply_batches(
        prepared,
        batch_dir=resolve_output_path(repo_dir, batch_dir),
        batch_size=batch_size,
        namespace=namespace,
        node_map=node_map,  # type: ignore[arg-type]
        catalog=catalog,
        signature_abort_threshold=threshold,
    )
    report["batch_manifest"] = batch_result["manifest"]

    if mode == "emit-only":
        emit_json_value(report)
        return

    execution = asyncio.run(
        execute_use_figma_calls(
            batch_result["calls"],
            resume_from=resume_from - 1,
            continue_on_error=continue_on_error,
            dry_run=False,
        )
    )
    report["execution"] = execution

    # F48: if any batch's runtime aborted on a class-level signature,
    # promote ONE F36 block to the human path and exit with the
    # operator-action code so wrappers can route the failure to a human.
    abort = _first_signature_abort(execution)
    if abort is not None:
        report["operator_action"] = {
            "signature": abort["signature"],
            "count": abort["count"],
            "sample_rows": abort.get("sample_rows") or [],
            "instruction": operator_action_for_signature(abort["signature"]),
        }
        emit_json_value(report)
        action = report["operator_action"]
        click.echo(
            f"⚠️  ACTION REQUIRED — {action['signature']} hit "
            f"{action['count']} times; aborting. {action['instruction']}",
            err=True,
        )
        ctx.exit(EXIT_OPERATOR_ACTION_REQUIRED)
    emit_json_value(report)


def _first_signature_abort(execution: Any) -> dict | None:
    """Walk an execution result for the first signatureAbort returned by a batch.

    The use_figma executor returns a list-of-batch-results; each batch's
    return value (the JS template's ``summary``) carries a
    ``signatureAbort`` field when the per-row loop bailed early. Returning
    the first occurrence is enough to drive the F36 surface — the
    threshold guarantees we are confident this signature dominates.
    """
    if not isinstance(execution, dict):
        return None
    for batch_result in execution.get("batches") or []:
        if not isinstance(batch_result, dict):
            continue
        result = batch_result.get("result")
        if isinstance(result, dict):
            abort = result.get("signatureAbort")
            if isinstance(abort, dict) and abort.get("signature"):
                return abort
    return None


def _resolve_library_filter(catalog: TokenCatalog, libraries: tuple[str, ...]) -> set[str] | None:
    if not libraries:
        return None
    matched: set[str] = set()
    for needle in libraries:
        lowered = needle.lower()
        for library_hash, library in catalog.libraries.items():
            if lowered in library_hash.lower() or lowered in (library.name or "").lower():
                matched.add(library_hash)
    if not matched:
        raise click.ClickException(
            "--library filter matched no libraries in the catalog: " + ", ".join(libraries)
        )
    return matched


def _emit_human_plan(report: dict) -> None:
    click.echo(f"mode: {report['mode']}")
    click.echo(f"file: {report['file_key']}")
    click.echo(f"page: {report['page_node_id']}")
    click.echo(f"fixes: {report['fixes']}")
    click.echo(f"refusals: {report['refusals']}")
    for reason, count in report["counts"]["refusals"].items():
        click.echo(f"  {reason}: {count}")
    # Surface a sample of refused rows in the human path too — counts alone
    # force operators to re-run with --json. (#167 review finding #9.)
    sample = report.get("refusal_sample") or []
    for entry in sample[:5]:
        row = entry.get("row") or {}
        # Make the most informative diagnostic fields visible per row.
        details: list[str] = []
        if "unrecognised_compact_row_fields" in row:
            details.append(f"unknown={row['unrecognised_compact_row_fields']}")
        if "missing_canonical_fields" in row:
            details.append(f"missing={row['missing_canonical_fields']}")
        if "did_you_mean_token_name" in row:
            details.append(f"did_you_mean={row['did_you_mean_token_name']!r}")
        details_text = "; ".join(details) if details else ""
        click.echo(
            f"    row {entry['row_index']}: {entry['reason']}"
            + (f" ({details_text})" if details_text else "")
        )
    if len(sample) > 5:
        click.echo(f"    … {len(sample) - 5} more (--remaining-out for full list)")
