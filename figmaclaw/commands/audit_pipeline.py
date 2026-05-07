"""figmaclaw audit-pipeline — read-only migration input linting."""

from __future__ import annotations

from pathlib import Path

import click

from figmaclaw.audit import build_pipeline_lint_report
from figmaclaw.commands.reporting import emit_json_report, resolve_output_path


@click.group("audit-pipeline")
def audit_pipeline_group() -> None:
    """Validate design-system migration pipeline inputs."""


@audit_pipeline_group.command("lint")
@click.option(
    "--component-map",
    "component_map_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="component_migration_map.v3.json to validate.",
)
@click.option(
    "--census",
    "census_paths",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    help="Optional figmaclaw _census.md target registry. Repeat for multiple files.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON report path. By default, no file is written.",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")
@click.pass_context
def audit_pipeline_lint_cmd(
    ctx: click.Context,
    component_map_path: Path,
    census_paths: tuple[Path, ...],
    out_path: Path | None,
    json_output: bool,
) -> None:
    """Lint migration inputs before a human/agent applies them."""
    repo_dir = Path(ctx.obj["repo_dir"])
    report = build_pipeline_lint_report(
        resolve_output_path(repo_dir, component_map_path),
        census_paths=[resolve_output_path(repo_dir, path) for path in census_paths],
    )
    report_data = report.model_dump(mode="json")

    if emit_json_report(
        ctx,
        repo_dir=repo_dir,
        report_data=report_data,
        out_path=out_path,
        json_output=json_output,
    ):
        return

    click.echo(f"component map: {component_map_path}")
    click.echo(f"rules: {report.rule_count}")
    click.echo(f"target registry: {report.target_registry_state}")
    for status, count in sorted(report.counts.items()):
        click.echo(f"{status}: {count}")
    click.echo(f"ok: {str(report.ok).lower()}")
