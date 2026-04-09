"""figmaclaw CLI entry point."""

from __future__ import annotations

import click

from figmaclaw.commands.apply_webhook import apply_webhook_cmd
from figmaclaw.commands.build_context import build_context_cmd
from figmaclaw.commands.census import census_cmd
from figmaclaw.commands.claude_run import claude_run_cmd
from figmaclaw.commands.diff import diff_cmd
from figmaclaw.commands.doctor import doctor_cmd
from figmaclaw.commands.image_urls import image_urls_cmd
from figmaclaw.commands.init import init_cmd
from figmaclaw.commands.inspect import inspect_cmd
from figmaclaw.commands.list_files import list_cmd
from figmaclaw.commands.mark_enriched import mark_enriched_cmd
from figmaclaw.commands.mark_stale import mark_stale_cmd
from figmaclaw.commands.pull import pull_cmd
from figmaclaw.commands.screenshots import screenshots_cmd
from figmaclaw.commands.self_cmd import self_group
from figmaclaw.commands.set_flows import set_flows_cmd
from figmaclaw.commands.stream_format import stream_format_cmd
from figmaclaw.commands.suggest_tokens import suggest_tokens_cmd
from figmaclaw.commands.sync import sync_cmd
from figmaclaw.commands.track import track_cmd
from figmaclaw.commands.webhooks import webhooks_group
from figmaclaw.commands.workflows_cmd import workflows_group
from figmaclaw.commands.write_body import write_body_cmd
from figmaclaw.commands.write_descriptions import write_descriptions_cmd


def _version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    import figmaclaw._build_info as _bi  # lazy: keeps module-level names mockable in tests

    short_sha = _bi.__commit__[:8] if _bi.__commit__ else "unknown"
    pr_info = f" · PR #{_bi.__pr__}" if _bi.__pr__ else ""
    click.echo(f"figmaclaw {_bi.__version__} ({short_sha}{pr_info})")
    first_line = _bi.__commit_message__.split("\n")[0].strip() if _bi.__commit_message__ else ""
    if first_line:
        click.echo(f"  {first_line}")
    ctx.exit()


@click.group()
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_version_callback,
    help="Show version and exit.",
)
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
cli.add_command(build_context_cmd)
cli.add_command(census_cmd)
cli.add_command(claude_run_cmd)
cli.add_command(diff_cmd)
cli.add_command(doctor_cmd)
cli.add_command(init_cmd)
cli.add_command(list_cmd)
cli.add_command(mark_enriched_cmd)
cli.add_command(mark_stale_cmd)
cli.add_command(inspect_cmd)
cli.add_command(pull_cmd)
cli.add_command(image_urls_cmd)
cli.add_command(screenshots_cmd)
cli.add_command(self_group)
cli.add_command(stream_format_cmd)
cli.add_command(set_flows_cmd)
cli.add_command(sync_cmd)
cli.add_command(track_cmd)
cli.add_command(webhooks_group)
cli.add_command(workflows_group)
cli.add_command(suggest_tokens_cmd)
cli.add_command(write_body_cmd)
cli.add_command(write_descriptions_cmd)
