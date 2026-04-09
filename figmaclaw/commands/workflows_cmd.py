"""Workflows commands: manage bundled workflow templates in target repos."""

from __future__ import annotations

import json
from pathlib import Path

import click

from figmaclaw.workflow_templates import (
    collect_workflow_status,
    copy_workflow_templates,
    workflow_template_files,
)


@click.group("workflows")
def workflows_group() -> None:
    """Manage bundled GitHub Actions workflow templates."""


@workflows_group.command("upgrade")
@click.option(
    "--repo-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Path to the target repository.",
)
@click.pass_context
def workflows_upgrade(ctx: click.Context, repo_dir: Path) -> None:
    """Re-copy bundled workflow templates to .github/workflows/."""
    written = copy_workflow_templates(repo_dir)

    json_mode = ctx.obj.get("json", False) if ctx.obj else False
    if json_mode:
        click.echo(
            json.dumps(
                {
                    "repo_dir": str(repo_dir),
                    "written": written,
                    "count": len(written),
                }
            )
        )
        return

    for name in written:
        click.echo(f"Updated .github/workflows/{name}")
    click.echo("Workflow templates upgraded successfully.")


@workflows_group.command("doctor")
@click.option(
    "--repo-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Path to the target repository.",
)
@click.option(
    "--strict/--no-strict",
    default=False,
    help="Return non-zero exit code if any managed workflow is missing/drifted.",
)
@click.pass_context
def workflows_doctor(ctx: click.Context, repo_dir: Path, strict: bool) -> None:
    """Report managed workflow template health for a target repository."""
    statuses = collect_workflow_status(repo_dir)
    healthy = all(s.healthy for s in statuses)

    json_mode = ctx.obj.get("json", False) if ctx.obj else False
    if json_mode:
        click.echo(
            json.dumps(
                {
                    "repo_dir": str(repo_dir),
                    "healthy": healthy,
                    "expected": list(workflow_template_files()),
                    "files": [
                        {
                            "name": s.name,
                            "exists": s.exists,
                            "managed": s.managed,
                            "in_sync": s.in_sync,
                            "healthy": s.healthy,
                        }
                        for s in statuses
                    ],
                }
            )
        )
    else:
        click.echo(f"Workflow doctor for: {repo_dir}")
        for s in statuses:
            if s.healthy:
                status = "ok"
            elif not s.exists:
                status = "missing"
            elif not s.managed:
                status = "unmanaged"
            else:
                status = "drifted"
            click.echo(f"- {s.name}: {status}")

        if healthy:
            click.echo("All managed workflow templates are present and up to date.")
        else:
            click.echo("Detected workflow template issues.")
            click.echo("Run `figmaclaw workflows upgrade` to repair managed files.")

    if strict and not healthy:
        raise click.ClickException("Workflow doctor failed: managed workflow issues found.")
