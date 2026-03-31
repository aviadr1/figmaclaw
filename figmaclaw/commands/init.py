"""figmaclaw init — set up CI/CD workflows in a consuming repository."""

from __future__ import annotations

import shutil
from pathlib import Path

import click


_TEMPLATES = ["figmaclaw-webhook.yaml", "figmaclaw-sync.yaml"]


@click.command("init")
@click.option(
    "--workflows-dir",
    default=".github/workflows",
    show_default=True,
    help="Directory to copy workflow files into.",
)
@click.option("--overwrite", is_flag=True, help="Overwrite existing workflow files.")
@click.pass_context
def init_cmd(ctx: click.Context, workflows_dir: str, overwrite: bool) -> None:
    """Copy figmaclaw workflow templates into .github/workflows/."""
    repo_dir = Path(ctx.obj["repo_dir"])
    dest_dir = repo_dir / workflows_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    templates_dir = Path(__file__).parent.parent / "templates"

    copied: list[str] = []
    skipped: list[str] = []

    for template_name in _TEMPLATES:
        src = templates_dir / template_name
        dest = dest_dir / template_name

        if dest.exists() and not overwrite:
            skipped.append(template_name)
            click.echo(f"  skipped (exists): {dest.relative_to(repo_dir)}")
            continue

        shutil.copy2(src, dest)
        copied.append(template_name)
        click.echo(f"  wrote: {dest.relative_to(repo_dir)}")

    if copied:
        click.echo(f"\nCopied {len(copied)} workflow(s). Next steps:")
        click.echo("  1. Set GitHub secrets: FIGMA_API_KEY, FIGMA_WEBHOOK_SECRET")
        click.echo("  2. Discover files:  figmaclaw list <team-id-or-url> --since 3m")
        click.echo("  3. Track a file:    figmaclaw track <file-key>")
        click.echo("  4. Commit and push .github/workflows/ and .figma-sync/")
    elif skipped:
        click.echo("\nAll workflow files already exist. Use --overwrite to replace them.")
