"""figmaclaw init — set up CI/CD workflows in a consuming repository."""

from __future__ import annotations

import shutil
from pathlib import Path

import click

_TEMPLATES = ["figmaclaw-webhook.yaml", "figmaclaw-sync.yaml"]
_PROXY_DIR = "webhook-proxy"


@click.command("init")
@click.option(
    "--workflows-dir",
    default=".github/workflows",
    show_default=True,
    help="Directory to copy workflow files into.",
)
@click.option("--overwrite", is_flag=True, help="Overwrite existing workflow files.")
@click.option(
    "--with-webhook-proxy",
    is_flag=True,
    help="Also copy the Cloudflare Worker webhook proxy template into workers/figma-webhook-proxy/.",
)
@click.pass_context
def init_cmd(
    ctx: click.Context, workflows_dir: str, overwrite: bool, with_webhook_proxy: bool
) -> None:
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

    if with_webhook_proxy:
        proxy_src = templates_dir / _PROXY_DIR
        proxy_dest = repo_dir / "workers" / "figma-webhook-proxy"
        if proxy_dest.exists() and not overwrite:
            skipped.append(_PROXY_DIR)
            click.echo(f"  skipped (exists): {proxy_dest.relative_to(repo_dir)}/")
        else:
            if proxy_dest.exists():
                shutil.rmtree(proxy_dest)
            shutil.copytree(proxy_src, proxy_dest)
            copied.append(_PROXY_DIR)
            click.echo(f"  wrote: {proxy_dest.relative_to(repo_dir)}/")

    if copied:
        click.echo(f"\nCopied {len(copied)} template(s). Next steps:")
        click.echo("  1. Set GitHub secrets: FIGMA_API_KEY, FIGMA_WEBHOOK_SECRET")
        click.echo("  2. Discover files:  figmaclaw list <team-id-or-url> --since 3m")
        click.echo("  3. Track a file:    figmaclaw track <file-key>")
        click.echo("  4. Commit and push .github/workflows/ and .figma-sync/")
        if with_webhook_proxy and _PROXY_DIR in copied:
            click.echo("  5. Edit workers/figma-webhook-proxy/wrangler.toml:")
            click.echo("       - Set GITHUB_REPO to your org/repo")
            click.echo("       - Run: wrangler kv namespace create DEBOUNCE")
            click.echo("       - Paste the KV namespace ID into wrangler.toml")
            click.echo("       - Run: wrangler secret put FIGMA_WEBHOOK_SECRET")
            click.echo("       - Run: wrangler secret put GITHUB_TOKEN")
            click.echo("       - Deploy: wrangler deploy")
    elif skipped:
        click.echo("\nAll templates already exist. Use --overwrite to replace them.")
