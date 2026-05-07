"""figmaclaw audit-page — read-only audit page migration checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import click

from figmaclaw.audit import (
    build_audit_check_report,
    build_audit_diagnose_report,
    load_json_file,
    load_palette,
)
from figmaclaw.commands._shared import require_figma_api_key
from figmaclaw.commands.reporting import emit_json_report, resolve_output_path
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_utils import write_json_if_changed


@click.group("audit-page")
def audit_page_group() -> None:
    """Inspect audit pages used during design-system migrations."""


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

    async with FigmaClient(api_key) as client:
        nodes = await client.get_nodes(file_key, [audit_page_id], depth=100)
    page = nodes.get(audit_page_id) or nodes.get(audit_page_id.replace(":", "-"))
    if not page:
        raise click.UsageError(f"Node {audit_page_id!r} not found in Figma REST response.")
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
    "--new-palette",
    "new_palette_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional JSON object mapping new-system hex values to labels.",
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
    new_palette_path: Path | None,
    out_path: Path | None,
    json_output: bool,
) -> None:
    """Classify unbound literal paints on an audit page."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()
    old_palette = load_palette(old_palette_path)
    new_palette = load_palette(new_palette_path)
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
    async with FigmaClient(api_key) as client:
        nodes = await client.get_nodes(file_key, [audit_page_id], depth=100)
    page = nodes.get(audit_page_id) or nodes.get(audit_page_id.replace(":", "-"))
    if not page:
        raise click.UsageError(f"Node {audit_page_id!r} not found in Figma REST response.")
    return build_audit_diagnose_report(
        page,
        audit_page_id=audit_page_id,
        old_palette=old_palette,
        new_palette=new_palette,
    )
