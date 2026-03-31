"""figmaclaw CLI entry point."""

from __future__ import annotations

import click

from figmaclaw import __version__
from figmaclaw.commands.apply_webhook import apply_webhook_cmd
from figmaclaw.commands.init import init_cmd
from figmaclaw.commands.list_files import list_cmd
from figmaclaw.commands.pull import pull_cmd
from figmaclaw.commands.track import track_cmd
from figmaclaw.commands.workflows_cmd import workflows_group


@click.group()
@click.version_option(version=__version__, package_name="figmaclaw")
@click.option("--json", "json_mode", is_flag=True, help="Output strict JSON for agents.")
@click.option("--verbose", "-v", count=True, help="Increase verbosity.")
@click.option("--quiet", "-q", count=True, help="Suppress non-essential output.")
@click.option(
    "--repo-dir",
    type=click.Path(file_okay=False, path_type=str),
    default=".",
    show_default=True,
    help="Path to the target git repository.",
)
@click.pass_context
def cli(ctx: click.Context, json_mode: bool, verbose: int, quiet: int, repo_dir: str) -> None:
    """figmaclaw — Figma → git semantic design memory for AI agents."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_mode
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    ctx.obj["repo_dir"] = repo_dir


cli.add_command(apply_webhook_cmd)
cli.add_command(init_cmd)
cli.add_command(list_cmd)
cli.add_command(pull_cmd)
cli.add_command(track_cmd)
cli.add_command(workflows_group)
