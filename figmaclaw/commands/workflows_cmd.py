"""Workflows commands: upgrade bundled workflow templates."""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import click


def _copy_workflow_files(repo_dir: Path) -> list[str]:
    """Copy bundled caller stubs to .github/workflows/, overwriting existing files.

    Returns a list of filenames that were written.
    """
    wf_dir = repo_dir / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)

    workflows_pkg = importlib.resources.files("figmaclaw") / "workflows"
    written = []
    for name in ("figmaclaw-sync.yaml", "figmaclaw-webhook.yaml"):
        src = workflows_pkg / name
        dst = wf_dir / name
        dst.write_text(src.read_text())
        written.append(name)
    return written


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
def workflows_upgrade(repo_dir: Path) -> None:
    """Re-copy bundled workflow stubs to .github/workflows/, overwriting existing files.

    Only the thin caller stubs are updated — CI logic lives in
    aviadr1/figmaclaw/.github/workflows/ and is picked up automatically.
    """
    written = _copy_workflow_files(repo_dir)
    for name in written:
        click.echo(f"Updated .github/workflows/{name}")
    click.echo("Workflow templates upgraded successfully.")
