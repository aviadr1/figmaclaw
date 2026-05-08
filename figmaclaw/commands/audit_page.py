"""figmaclaw audit-page — read-only audit page migration checks."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import click
from pydantic import ValidationError

from figmaclaw.audit import (
    build_audit_check_report,
    build_audit_diagnose_report,
    load_json_file,
    load_palette,
    load_palette_from_ds_catalog,
    parse_palette_entries,
)
from figmaclaw.audit_page_primitives import (
    ALLOWED_CLONE_REST_TYPES,
    annotate_component_keys,
    build_idmap_report,
    clone_request_receipt,
    default_clone_title,
    iter_node_records,
    load_jsonl_records,
    looks_like_inactive_page_name,
    record_to_jsonl_line,
    render_clone_script,
)
from figmaclaw.audit_page_swap import (
    AUDIT_PAGE_SWAP_SCHEMA_VERSION,
    SwapRow,
    load_swap_manifest,
    render_swap_script_from_writer_rows,
    row_to_writer_dict,
)
from figmaclaw.commands._shared import require_figma_api_key
from figmaclaw.commands.reporting import (
    emit_json_report,
    emit_json_value,
    is_stdout_path,
    resolve_output_path,
    resolve_repo_path,
    write_json_output,
)
from figmaclaw.figma_client import FigmaClient, normalize_node_id
from figmaclaw.figma_utils import write_json_if_changed
from figmaclaw.use_figma_batches import use_figma_batch_options, write_use_figma_batches
from figmaclaw.use_figma_exec import execute_use_figma_calls


@click.group("audit-page")
def audit_page_group() -> None:
    """Inspect audit pages used during design-system migrations."""


def _response_node(nodes: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = nodes.get(normalize_node_id(node_id))
    if not isinstance(node, dict) or not node:
        raise click.UsageError(f"Node {node_id!r} not found in Figma REST response.")
    return node


async def _fetch_node(
    api_key: str,
    file_key: str,
    node_id: str,
    *,
    depth: int | None = 1,
    geometry: str | None = None,
) -> dict[str, Any]:
    async with FigmaClient(api_key) as client:
        nodes = await client.get_nodes(file_key, [node_id], depth=depth, geometry=geometry)
    return _response_node(nodes, node_id)


async def _fetch_node_for_jsonl(
    api_key: str,
    file_key: str,
    node_id: str,
    *,
    depth: int | None = 1,
    geometry: str | None = None,
) -> dict[str, Any]:
    async with FigmaClient(api_key) as client:
        payload = await client.get_nodes_response(
            file_key,
            [node_id],
            depth=depth,
            geometry=geometry,
        )
    nodes = {
        normalize_node_id(key): value.get("document", {})
        for key, value in (payload.get("nodes") or {}).items()
        if isinstance(value, dict)
    }
    node = _response_node(nodes, node_id)
    components = payload.get("components") or {}
    if isinstance(components, dict):
        annotate_component_keys(node, components)
    return node


@audit_page_group.command("fetch-nodes")
@click.argument("file_key")
@click.argument("node_id")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSONL output path. Use '-' for stdout. By default, records are written to stdout.",
)
@click.option(
    "--geometry",
    default="paths",
    show_default=True,
    help="Optional Figma REST geometry parameter.",
)
@click.option(
    "--depth",
    type=int,
    help="Optional Figma REST depth. By default, Figma returns the full subtree.",
)
@click.pass_context
def audit_page_fetch_nodes_cmd(
    ctx: click.Context,
    file_key: str,
    node_id: str,
    out_path: Path | None,
    geometry: str,
    depth: int | None,
) -> None:
    """Fetch a node subtree from Figma REST and emit migration JSONL records."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    node = asyncio.run(
        _fetch_node_for_jsonl(
            api_key,
            file_key,
            node_id,
            depth=depth,
            geometry=geometry or None,
        )
    )
    records = iter_node_records(node, root_node_id=normalize_node_id(node_id))
    record_count = 0

    if out_path is None or is_stdout_path(out_path):
        for record in records:
            click.echo(record_to_jsonl_line(record), nl=False)
            record_count += 1
    else:
        resolved_out = resolve_output_path(repo_dir, out_path)
        resolved_out.parent.mkdir(parents=True, exist_ok=True)
        with resolved_out.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(record_to_jsonl_line(record))
                record_count += 1
    click.echo(f"emitted {record_count} node records", err=True)


@audit_page_group.command("build-idmap")
@click.option(
    "--src",
    "src_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Source nodes JSONL in DFS order.",
)
@click.option(
    "--dst",
    "dst_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination audit/clone nodes JSONL in DFS order.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Output idmap JSON path. Use '-' for stdout.",
)
@click.option(
    "--report-out",
    "report_out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON report path for counts and divergences. Use '-' for stdout.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Write the idmap, but exit non-zero when structural divergences are reported.",
)
@click.option(
    "--allow-divergent",
    is_flag=True,
    help="Write a partial idmap even when structural divergences are reported.",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON report.")
@click.pass_context
def audit_page_build_idmap_cmd(
    ctx: click.Context,
    src_path: Path,
    dst_path: Path,
    out_path: Path,
    report_out_path: Path | None,
    strict: bool,
    allow_divergent: bool,
    json_output: bool,
) -> None:
    """Build src-to-clone idmap JSON by DFS-zipping two fetch-nodes JSONL files."""
    repo_dir = Path(ctx.obj["repo_dir"])
    idmap_to_stdout = is_stdout_path(out_path)
    report_to_stdout = is_stdout_path(report_out_path)
    if idmap_to_stdout and (json_output or ctx.obj.get("json")):
        raise click.UsageError("--out - cannot be combined with --json; both need stdout.")
    if idmap_to_stdout and report_to_stdout:
        raise click.UsageError("--out - cannot be combined with --report-out -.")
    resolved_src = resolve_repo_path(repo_dir, src_path)
    resolved_dst = resolve_repo_path(repo_dir, dst_path)
    if not resolved_src.exists():
        raise click.UsageError(f"{resolved_src}: source JSONL does not exist")
    if not resolved_dst.exists():
        raise click.UsageError(f"{resolved_dst}: destination JSONL does not exist")

    try:
        src_records = load_jsonl_records(resolved_src)
        dst_records = load_jsonl_records(resolved_dst)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.UsageError(str(exc)) from exc

    idmap, report = build_idmap_report(src_records, dst_records)
    should_write_idmap = bool(report["ok"] or strict or allow_divergent)
    report["idmap_written"] = should_write_idmap
    if should_write_idmap:
        if report["ok"]:
            report["idmap_write_reason"] = "clean"
        elif strict:
            report["idmap_write_reason"] = "strict_divergent"
        else:
            report["idmap_write_reason"] = "allow_divergent"
        write_json_output(repo_dir, out_path, idmap)
    else:
        report["idmap_write_reason"] = "divergence_refused"

    if report_out_path is not None:
        write_json_output(repo_dir, report_out_path, report)

    emitted_json = emit_json_report(
        ctx,
        repo_dir=repo_dir,
        report_data=report,
        out_path=None,
        json_output=json_output,
    )
    if not emitted_json:
        click.echo(f"src records: {report['src_records']}", err=idmap_to_stdout)
        click.echo(f"dst records: {report['dst_records']}", err=idmap_to_stdout)
        click.echo(f"idmap entries: {report['idmap_entries']}", err=idmap_to_stdout)
        click.echo(f"divergences: {report['divergence_count']}", err=idmap_to_stdout)
        click.echo(f"idmap written: {str(report['idmap_written']).lower()}", err=idmap_to_stdout)
        for divergence in report["divergences"][:5]:
            click.echo(f"  {divergence}", err=idmap_to_stdout)
        if len(report["divergences"]) > 5:
            click.echo(
                f"  ... and {len(report['divergences']) - 5} more",
                err=idmap_to_stdout,
            )

    if not report["ok"] and not should_write_idmap:
        click.echo(
            "refusing to write idmap: "
            f"{report['divergence_count']} divergence(s); pass --allow-divergent "
            "to write the partial map and exit 0, or --strict to write it and exit 1.",
            err=True,
        )
    elif not report["ok"] and strict:
        click.echo(
            "wrote partial idmap: "
            f"{report['divergence_count']} divergence(s); --strict exits non-zero.",
            err=True,
        )

    if not report["ok"] and (strict or not allow_divergent):
        ctx.exit(1)


@audit_page_group.command("emit-clone-script")
@click.argument("file_key")
@click.argument("source_node_id")
@click.option(
    "--destination-page-id",
    "--target-page-id",
    help="Existing page to clone into. Without this, the generated script creates a new page.",
)
@click.option("--title", help="New page title when creating a page.")
@click.option("--namespace", default="linear_git_migration", show_default=True)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional generated Plugin API JS path. Use '-' for stdout. By default, JS is written to stdout.",
)
@click.option(
    "--receipt",
    "receipt_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON request receipt path.",
)
@click.option(
    "--strict-source",
    is_flag=True,
    help=(
        "Refuse to emit when the source node looks like a previous audit-page "
        "output (e.g. starts with '🛠 Audit'). Pair with --allow-audit-page-source "
        "to override on purpose."
    ),
)
@click.option(
    "--allow-audit-page-source",
    is_flag=True,
    help="Suppress the audit-page warning and let --strict-source pass through.",
)
@click.pass_context
def audit_page_emit_clone_script_cmd(
    ctx: click.Context,
    file_key: str,
    source_node_id: str,
    destination_page_id: str | None,
    title: str | None,
    namespace: str,
    out_path: Path | None,
    receipt_path: Path | None,
    strict_source: bool,
    allow_audit_page_source: bool,
) -> None:
    """Emit use_figma-clean JS that clones a page, frame, or section and returns a result."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    source_node, destination_page = asyncio.run(
        _fetch_clone_metadata(api_key, file_key, source_node_id, destination_page_id)
    )

    source_type = source_node.get("type")
    if source_type not in ALLOWED_CLONE_REST_TYPES:
        allowed = ", ".join(sorted(ALLOWED_CLONE_REST_TYPES))
        raise click.UsageError(f"Expected source node type {allowed}; got {source_type}.")
    if destination_page is not None and destination_page.get("type") != "CANVAS":
        raise click.UsageError(
            f"Expected destination page type CANVAS; got {destination_page.get('type')}."
        )

    source_name = source_node.get("name")
    source_looks_inactive = looks_like_inactive_page_name(
        source_name if isinstance(source_name, str) else None
    )
    if source_looks_inactive and not allow_audit_page_source:
        warning = (
            f"WARNING: source node {source_node_id} ({source_name!r}) looks like a "
            "non-active page (audit/playground/archive/wip/draft/etc.). Cloning a "
            "partially-migrated, deprecated, or scratch tree silently halves the "
            "OLD inventory and produces a wrong rules/component-map authoring set. "
            "If you meant the original source, re-run with that node id; otherwise "
            "pass --allow-audit-page-source."
        )
        click.echo(warning, err=True)
        if strict_source:
            raise click.ClickException(
                "refusing to clone an apparently-inactive source under "
                "--strict-source; pass --allow-audit-page-source to override."
            )

    target_title = title or (
        str(destination_page.get("name"))
        if destination_page
        else default_clone_title(str(source_node.get("name") or "source"))
    )
    js = render_clone_script(
        file_key=file_key,
        source_node_id=normalize_node_id(source_node_id),
        title=target_title,
        namespace=namespace,
        destination_page_id=normalize_node_id(destination_page_id) if destination_page_id else None,
    )

    resolved_out: Path | None = None
    if out_path is None or is_stdout_path(out_path):
        click.echo(js, nl=False)
    else:
        resolved_out = resolve_output_path(repo_dir, out_path)
        resolved_out.parent.mkdir(parents=True, exist_ok=True)
        resolved_out.write_text(js, encoding="utf-8")

    receipt = clone_request_receipt(
        file_key=file_key,
        source_node=source_node,
        title=target_title,
        namespace=namespace,
        generated_js=str(out_path) if out_path else None,
        destination_page=destination_page,
    )
    if receipt_path is not None:
        write_json_if_changed(resolve_output_path(repo_dir, receipt_path), receipt)

    click.echo(f"source: {source_node.get('name')} ({source_node_id}, {source_type})", err=True)
    if destination_page is not None:
        click.echo(
            f"destination page: {destination_page.get('name')} ({destination_page_id})",
            err=True,
        )
    else:
        click.echo(f"new page title: {target_title}", err=True)
    if resolved_out is not None:
        click.echo(f"generated: {resolved_out}", err=True)


async def _fetch_clone_metadata(
    api_key: str,
    file_key: str,
    source_node_id: str,
    destination_page_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    node_ids = [source_node_id]
    if destination_page_id is not None:
        node_ids.append(destination_page_id)
    async with FigmaClient(api_key) as client:
        nodes = await client.get_nodes(file_key, node_ids, depth=1)
    source = _response_node(nodes, source_node_id)
    destination = _response_node(nodes, destination_page_id) if destination_page_id else None
    return source, destination


@audit_page_group.command("check")
@click.argument("file_key")
@click.argument("audit_page_id")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Binding intent manifest JSON, e.g. bindings_for_figma.json.",
)
@click.option(
    "--idmap",
    "idmap_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Source-to-audit clone node id map JSON.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON report path. Use '-' for stdout. By default, no file is written.",
)
@click.option(
    "--remaining-out",
    "remaining_out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional path for the input manifest rows that are not yet bound.",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")
@click.pass_context
def audit_page_check_cmd(
    ctx: click.Context,
    file_key: str,
    audit_page_id: str,
    manifest_path: Path,
    idmap_path: Path,
    out_path: Path | None,
    remaining_out_path: Path | None,
    json_output: bool,
) -> None:
    """Check whether intended clone properties are variable-bound."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    try:
        report, remaining = asyncio.run(
            _run_check(
                api_key,
                file_key,
                audit_page_id,
                resolve_repo_path(repo_dir, manifest_path),
                resolve_repo_path(repo_dir, idmap_path),
            )
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    report_data = report.model_dump(mode="json")

    if remaining_out_path is not None:
        write_json_if_changed(
            resolve_output_path(repo_dir, remaining_out_path), {"rows": remaining}
        )

    if emit_json_report(
        ctx,
        repo_dir=repo_dir,
        report_data=report_data,
        out_path=out_path,
        json_output=json_output,
    ):
        return

    click.echo(f"audit page: {audit_page_id}")
    click.echo(f"manifest rows: {report.manifest_rows}")
    for status, count in sorted(report.counts.items()):
        click.echo(f"{status}: {count}")
    if report.ok:
        click.echo("ok: true")
    else:
        click.echo(f"ok: false ({len(report.misses)} finding(s))")


async def _run_check(
    api_key: str,
    file_key: str,
    audit_page_id: str,
    manifest_path: Path,
    idmap_path: Path,
) -> tuple[Any, list[dict[str, Any]]]:
    manifest_payload = load_json_file(manifest_path)
    if not isinstance(manifest_payload, list):
        raise click.UsageError(f"{manifest_path}: manifest must be a JSON list")
    idmap_payload = load_json_file(idmap_path)
    if not isinstance(idmap_payload, dict):
        raise click.UsageError(f"{idmap_path}: idmap must be a JSON object")
    idmap = {str(key): str(value) for key, value in idmap_payload.items() if value is not None}

    page = await _fetch_node(api_key, file_key, audit_page_id, depth=100)
    return build_audit_check_report(
        page,
        audit_page_id=audit_page_id,
        manifest_rows=manifest_payload,
        idmap=idmap,
    )


@audit_page_group.command("diagnose")
@click.argument("file_key")
@click.argument("audit_page_id")
@click.option(
    "--old-palette",
    "old_palette_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON object mapping old-system hex values to labels.",
)
@click.option(
    "--old-palette-entry",
    "--old-palette-extra",
    "old_palette_entries",
    multiple=True,
    metavar="HEX=LABEL",
    help="Old-system palette entry. Repeatable; entries override duplicate JSON keys.",
)
@click.option(
    "--new-palette",
    "new_palette_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON object mapping new-system hex values to labels.",
)
@click.option(
    "--new-palette-from-ds-catalog",
    "new_palette_catalog_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional ds_catalog.json path to derive new-system color variables from.",
)
@click.option(
    "--new-palette-entry",
    "--new-palette-extra",
    "new_palette_entries",
    multiple=True,
    metavar="HEX=LABEL",
    help="New-system palette entry. Repeatable; entries override duplicate JSON keys.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON report path. Use '-' for stdout. By default, no file is written.",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")
@click.pass_context
def audit_page_diagnose_cmd(
    ctx: click.Context,
    file_key: str,
    audit_page_id: str,
    old_palette_path: Path | None,
    old_palette_entries: tuple[str, ...],
    new_palette_path: Path | None,
    new_palette_catalog_path: Path | None,
    new_palette_entries: tuple[str, ...],
    out_path: Path | None,
    json_output: bool,
) -> None:
    """Classify unbound literal paints on an audit page."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    try:
        old_palette = {
            **load_palette(
                resolve_repo_path(repo_dir, old_palette_path) if old_palette_path else None
            ),
            **parse_palette_entries(old_palette_entries),
        }
        catalog_palette = (
            load_palette_from_ds_catalog(resolve_repo_path(repo_dir, new_palette_catalog_path))
            if new_palette_catalog_path
            else {}
        )
        new_palette = {
            **catalog_palette,
            **load_palette(
                resolve_repo_path(repo_dir, new_palette_path) if new_palette_path else None
            ),
            **parse_palette_entries(new_palette_entries),
        }
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    report = asyncio.run(_run_diagnose(api_key, file_key, audit_page_id, old_palette, new_palette))
    report_data = report.model_dump(mode="json")

    if emit_json_report(
        ctx,
        repo_dir=repo_dir,
        report_data=report_data,
        out_path=out_path,
        json_output=json_output,
    ):
        return

    click.echo(f"audit page: {audit_page_id}")
    click.echo(f"bound paints: {report.bound_paints}")
    click.echo(f"unbound paints: {report.unbound_paints}")
    click.echo(f"unique unbound hex: {report.unique_unbound_hex}")
    for status, count in sorted(report.counts.items()):
        if not status.startswith("hex:"):
            click.echo(f"{status}: {count}")
    click.echo(f"ok: {str(report.ok).lower()}")


async def _run_diagnose(
    api_key: str,
    file_key: str,
    audit_page_id: str,
    old_palette: dict[str, str],
    new_palette: dict[str, str],
) -> Any:
    page = await _fetch_node(api_key, file_key, audit_page_id, depth=100)
    return build_audit_diagnose_report(
        page,
        audit_page_id=audit_page_id,
        old_palette=old_palette,
        new_palette=new_palette,
    )


# ── audit-page swap ───────────────────────────────────────────────────────────


@audit_page_group.command("swap")
@click.argument("file_key")
@click.argument("audit_page_id")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help=(
        "Swap manifest JSON. Either a versioned wrapper "
        "({schema_version, kind, rows: [...]}) or a bare list of swap rows "
        "({src, newKey, variants, props, preserveText, preserveSizing}). "
        "Build via the migration's resolver script (e.g. build_swap_manifest.py)."
    ),
)
@click.option(
    "--namespace",
    default="linear_git_migration",
    show_default=True,
    help="SharedPluginData namespace where the audit page's idMap lives.",
)
@use_figma_batch_options(default_batch_size=50)
@click.pass_context
def audit_page_swap_cmd(
    ctx: click.Context,
    file_key: str,
    audit_page_id: str,
    manifest_path: Path,
    namespace: str,
    batch_dir: Path | None,
    batch_size: int,
    mode: str,
    resume_from: int,
    continue_on_error: bool,
    json_output: bool,
) -> None:
    """Emit / execute component-instance swaps on an audit page.

    The emitted JS template is F17/F22/F30 -compliant: never .detach(), never
    throw on partial failure, returns aggregate per-row stats. Per swap row it
    imports the new component_set, picks the variant child by axis match,
    creates an instance, copies preserve-listed props, inserts at the OLD
    parent's index, and removes the OLD instance. The audit page's idMap is
    updated so subsequent apply-tokens runs target the NEW instances.

    Default mode is dry-run; use ``--emit-only`` for deterministic batch
    files or ``--execute`` to run them.

    Recompose-local rules from the component-migration map are NOT consumed
    here — they require building a new local component first (Tier-3 work).
    Pass only ``swap_strategy=direct`` rows; the consumer-repo resolver
    (``scripts/build_swap_manifest.py``) already filters them out.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if batch_size <= 0:
        raise click.UsageError("--batch-size must be > 0")
    if resume_from < 1:
        raise click.UsageError("--resume-from must be >= 1")

    resolved_manifest = resolve_repo_path(repo_dir, manifest_path)
    try:
        payload = load_json_file(resolved_manifest)
        manifest = load_swap_manifest(payload)
    except (OSError, ValueError, ValidationError) as exc:
        raise click.UsageError(str(exc)) from exc

    # Reconcile manifest scope with CLI flags. CLI flags win when present.
    if manifest.file_key and manifest.file_key != file_key:
        raise click.UsageError(
            f"manifest file_key {manifest.file_key!r} does not match CLI file_key {file_key!r}"
        )
    if manifest.page_node_id and manifest.page_node_id != audit_page_id:
        raise click.UsageError(
            f"manifest page_node_id {manifest.page_node_id!r} does not match "
            f"CLI audit_page_id {audit_page_id!r}"
        )
    if manifest.namespace and manifest.namespace != namespace:
        # Manifest-pinned namespace beats the CLI default; only error when
        # they disagree explicitly.
        raise click.UsageError(
            f"manifest namespace {manifest.namespace!r} does not match "
            f"CLI --namespace {namespace!r}"
        )

    rows = manifest.rows
    report = _swap_plan_report(rows, mode=mode, file_key=file_key, audit_page_id=audit_page_id)

    if mode == "dry-run":
        if json_output or ctx.obj.get("json"):
            emit_json_value(report)
            return
        _emit_human_swap_plan(report)
        return

    if not rows:
        emit_json_value(report)
        raise click.ClickException("refusing to emit swap batches: 0 rows")

    # Surface plan warnings on stderr in non-dry-run modes too — operators
    # running --execute should not have to re-run with --dry-run to learn
    # that the manifest has structural problems. (#167 review finding #11.)
    for warning in report.get("warnings") or []:
        click.echo(f"warning: {warning}", err=True)

    # Hard refuse when EVERY row lacks oldCid: that means the resolver could
    # not link any row back to a published OLD componentId, so the swap is
    # almost certainly being run against the wrong manifest or wrong page.
    if rows and all(row.old_component_id is None for row in rows):
        raise click.ClickException(
            "refusing to emit/execute: every row in the manifest lacks oldCid; "
            "the resolver couldn't link any row to a published OLD componentId. "
            "Re-check the source page or rebuild the manifest."
        )

    if batch_dir is None:
        raise click.UsageError("--batch-dir is required for --emit-only and --execute")

    batch_result = _write_swap_batches(
        rows,
        batch_dir=resolve_output_path(repo_dir, batch_dir),
        batch_size=batch_size,
        namespace=namespace,
        file_key=file_key,
        audit_page_id=audit_page_id,
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
    emit_json_value(report)


def _swap_plan_warnings(rows: list[SwapRow]) -> list[str]:
    """Collect manifest-shape warnings the operator should see at dry-run time.

    These are not lint *errors* — the rules are still runnable — but they
    flag patterns that almost always indicate misuse:

    * rows missing ``oldCid`` (the resolver couldn't identify the OLD
      componentId, so the rule's variant axes can't be cross-checked)
    * rows whose ``variants`` mapping is empty (the swap will fall back to
      ``defaultVariant``, which is rarely what the operator intended)
    """
    warnings: list[str] = []
    missing_old = sum(1 for row in rows if not row.old_component_id)
    if missing_old:
        warnings.append(
            f"{missing_old} row(s) have no oldCid; resolver couldn't link them "
            "back to a published OLD componentId"
        )
    no_variants = sum(1 for row in rows if not row.variants)
    if no_variants:
        warnings.append(
            f"{no_variants} row(s) have an empty variants mapping; the swap "
            "will fall back to defaultVariant — verify that's intended"
        )
    return warnings


def _swap_plan_report(
    rows: list[SwapRow],
    *,
    mode: str,
    file_key: str,
    audit_page_id: str,
) -> dict[str, Any]:
    """Stable plan report for swap dry-run / emit-only modes."""
    new_keys = Counter(row.new_key for row in rows)
    old_cids = Counter(row.old_component_id or "<unknown>" for row in rows)
    return {
        "schema_version": AUDIT_PAGE_SWAP_SCHEMA_VERSION,
        "mode": mode,
        "file_key": file_key,
        "audit_page_id": audit_page_id,
        "rows": len(rows),
        "unique_new_keys": len(new_keys),
        "unique_old_component_ids": len(old_cids),
        "by_new_key": dict(new_keys),
        "by_old_component_id": dict(old_cids),
        "warnings": _swap_plan_warnings(rows),
    }


def _emit_human_swap_plan(report: dict[str, Any]) -> None:
    click.echo(f"mode: {report['mode']}")
    click.echo(f"file: {report['file_key']}")
    click.echo(f"audit page: {report['audit_page_id']}")
    click.echo(f"rows: {report['rows']}")
    click.echo(f"unique new keys: {report['unique_new_keys']}")
    click.echo(f"unique OLD componentIds: {report['unique_old_component_ids']}")
    for key, count in sorted(report["by_new_key"].items(), key=lambda x: -x[1])[:5]:
        click.echo(f"  {key}: {count}")
    warnings = report.get("warnings") or []
    if warnings:
        click.echo("warnings:", err=True)
        for warning in warnings:
            click.echo(f"  {warning}", err=True)


def _write_swap_batches(
    rows: list[SwapRow],
    *,
    batch_dir: Path,
    batch_size: int,
    namespace: str,
    file_key: str,
    audit_page_id: str,
) -> dict[str, Any]:
    return write_use_figma_batches(
        rows,
        batch_dir=batch_dir,
        batch_size=batch_size,
        file_name_prefix="swap-batch",
        file_key=file_key,
        row_to_dict=row_to_writer_dict,
        render_js=lambda writer_rows: render_swap_script_from_writer_rows(
            page_node_id=audit_page_id,
            namespace=namespace,
            writer_rows=writer_rows,
        ),
        description_prefix="audit-page swap batch",
        manifest_extras={
            "schema_version": AUDIT_PAGE_SWAP_SCHEMA_VERSION,
            "kind": "figmaclaw.audit_page_swap.batch_manifest",
            "file_key": file_key,
            "page_node_id": audit_page_id,
            "namespace": namespace,
        },
    )
