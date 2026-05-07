"""figmaclaw audit-page — read-only audit page migration checks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click

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
    records_to_jsonl,
    render_clone_script,
)
from figmaclaw.commands._shared import require_figma_api_key
from figmaclaw.commands.reporting import emit_json_report, resolve_output_path, resolve_repo_path
from figmaclaw.figma_client import FigmaClient, normalize_node_id
from figmaclaw.figma_utils import write_json_if_changed


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
    help="Optional JSONL output path. By default, records are written to stdout.",
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
    records = list(iter_node_records(node, root_node_id=normalize_node_id(node_id)))
    payload = records_to_jsonl(records)

    if out_path is None:
        click.echo(payload, nl=False)
    else:
        resolved_out = resolve_output_path(repo_dir, out_path)
        resolved_out.parent.mkdir(parents=True, exist_ok=True)
        resolved_out.write_text(payload, encoding="utf-8")
    click.echo(f"emitted {len(records)} node records", err=True)


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
    help="Output idmap JSON path.",
)
@click.option(
    "--report-out",
    "report_out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON report path for counts and divergences.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero on divergence, including with --allow-divergent.",
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
    should_write_idmap = bool(report["ok"] or allow_divergent)
    report["idmap_written"] = should_write_idmap
    if should_write_idmap:
        report["idmap_write_reason"] = "clean" if report["ok"] else "allow_divergent"
        write_json_if_changed(resolve_output_path(repo_dir, out_path), idmap)
    else:
        report["idmap_write_reason"] = "divergence_refused"

    if report_out_path is not None:
        write_json_if_changed(resolve_output_path(repo_dir, report_out_path), report)

    emitted_json = emit_json_report(
        ctx,
        repo_dir=repo_dir,
        report_data=report,
        out_path=None,
        json_output=json_output,
    )
    if not emitted_json:
        click.echo(f"src records: {report['src_records']}")
        click.echo(f"dst records: {report['dst_records']}")
        click.echo(f"idmap entries: {report['idmap_entries']}")
        click.echo(f"divergences: {report['divergence_count']}")
        click.echo(f"idmap written: {str(report['idmap_written']).lower()}")
        for divergence in report["divergences"][:5]:
            click.echo(f"  {divergence}")
        if len(report["divergences"]) > 5:
            click.echo(f"  ... and {len(report['divergences']) - 5} more")

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
    help="Optional generated Plugin API JS path. By default, JS is written to stdout.",
)
@click.option(
    "--receipt",
    "receipt_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON request receipt path.",
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
) -> None:
    """Emit Plugin API JS that clones a page, frame, or section for audit work."""
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
    if out_path is None:
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
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Binding intent manifest JSON, e.g. bindings_for_figma.json.",
)
@click.option(
    "--idmap",
    "idmap_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Source-to-audit clone node id map JSON.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON report path. By default, no file is written.",
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
    report, remaining = asyncio.run(
        _run_check(api_key, file_key, audit_page_id, manifest_path, idmap_path)
    )
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
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
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
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
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
    help="Optional JSON report path. By default, no file is written.",
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
            **load_palette(old_palette_path),
            **parse_palette_entries(old_palette_entries),
        }
        catalog_palette = (
            load_palette_from_ds_catalog(resolve_repo_path(repo_dir, new_palette_catalog_path))
            if new_palette_catalog_path
            else {}
        )
        new_palette = {
            **catalog_palette,
            **load_palette(new_palette_path),
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
