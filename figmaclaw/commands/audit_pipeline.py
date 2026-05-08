"""figmaclaw audit-pipeline — read-only migration input linting."""

from __future__ import annotations

import json
from pathlib import Path

import click

from figmaclaw.audit import build_pipeline_lint_report
from figmaclaw.commands.reporting import emit_json_report, resolve_repo_path


@click.group("audit-pipeline")
def audit_pipeline_group() -> None:
    """Validate design-system migration pipeline inputs."""


@audit_pipeline_group.command("lint")
@click.option(
    "--component-map",
    "component_map_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="component_migration_map.v3.json to validate.",
)
@click.option(
    "--census",
    "census_paths",
    type=click.Path(dir_okay=False, path_type=Path),
    multiple=True,
    help="Optional figmaclaw _census.md target registry. Repeat for multiple files.",
)
@click.option(
    "--variants",
    "variants_paths",
    type=click.Path(dir_okay=False, path_type=Path),
    multiple=True,
    help=(
        "Optional variant-taxonomy sidecar JSON file (component_set key → "
        "{name, axes: {<axis>: {values: [...]}}}). When provided, lint verifies "
        "every variant_mapping axis name and value, and flags missing OLD-axis "
        "coverage. Repeat for multiple files."
    ),
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional JSON report path. Use '-' for stdout. By default, no file is written.",
)
@click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")
@click.pass_context
def audit_pipeline_lint_cmd(
    ctx: click.Context,
    component_map_path: Path,
    census_paths: tuple[Path, ...],
    variants_paths: tuple[Path, ...],
    out_path: Path | None,
    json_output: bool,
) -> None:
    """Lint migration inputs before a human/agent applies them."""
    repo_dir = Path(ctx.obj["repo_dir"])
    try:
        report = build_pipeline_lint_report(
            resolve_repo_path(repo_dir, component_map_path),
            census_paths=[resolve_repo_path(repo_dir, path) for path in census_paths],
            variants_paths=[resolve_repo_path(repo_dir, path) for path in variants_paths],
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise click.UsageError(str(exc)) from exc
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
    # Print the actual finding messages so operators don't have to re-run
    # with --json to learn what failed. Cap at 20 lines so a manifest with
    # 200 broken rules doesn't drown the terminal — the JSON output is the
    # complete record. (Issue #167 review finding #9.)
    findings = report.findings
    show_limit = 20
    for finding in findings[:show_limit]:
        # Prepend the rule's old_component_set name when known so operators
        # don't have to cross-reference rules[i] back to the source map.
        # (#167 review-3 finding #8.)
        label = f" ({finding.rule_label})" if finding.rule_label else ""
        click.echo(f"  [{finding.status}]{label} {finding.message}")
    if len(findings) > show_limit:
        click.echo(f"  … {len(findings) - show_limit} more (use --json for full list)")
    click.echo(f"ok: {str(report.ok).lower()}")
