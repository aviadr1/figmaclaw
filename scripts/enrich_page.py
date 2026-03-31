"""CLI script: fetch one Figma page, run LLM enrichment, print rendered markdown.

Usage:
    uv run python scripts/enrich_page.py <file_key> <page_node_id>

Env vars required:
    FIGMA_API_KEY
    ANTHROPIC_API_KEY
"""

from __future__ import annotations

import asyncio
import os
import sys

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_llm import enrich_page_with_descriptions
from figmaclaw.figma_models import from_page_node
from figmaclaw.figma_paths import slugify
from figmaclaw.figma_render import render_page
from figmaclaw.figma_sync_state import PageEntry


async def _run(file_key: str, page_node_id: str) -> None:
    figma_key = os.environ.get("FIGMA_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not figma_key:
        raise click.ClickException("FIGMA_API_KEY is not set")
    if not anthropic_key:
        raise click.ClickException("ANTHROPIC_API_KEY is not set")

    async with FigmaClient(figma_key) as client:
        meta = await client.get_file_meta(file_key)
        file_name = meta.get("name", file_key)
        click.echo(f"File: {file_name}", err=True)

        page_node = await client.get_page(file_key, page_node_id)
        page = from_page_node(page_node, file_key=file_key, file_name=file_name)
        page = page.model_copy(update={"page_slug": slugify(page.page_name)})
        n_frames = sum(len(s.frames) for s in page.sections)
        click.echo(f"Page: {page.page_name} — {len(page.sections)} sections, {n_frames} frames", err=True)

    from anthropic import AsyncAnthropic
    anthropic = AsyncAnthropic(api_key=anthropic_key)
    click.echo("Running LLM enrichment...", err=True)

    try:
        enriched = await enrich_page_with_descriptions(anthropic, page)
    except Exception as exc:
        raise click.ClickException(f"LLM enrichment failed: {exc}") from exc

    described = sum(1 for s in enriched.sections for f in s.frames if f.description)
    click.echo(f"Described: {described}/{n_frames} frames, {len(enriched.flows)} flows", err=True)

    entry = PageEntry(
        page_name=enriched.page_name,
        page_slug=enriched.page_slug,
        md_path=f"figma/{file_key}/pages/{enriched.page_slug}.md",
        page_hash="preview",
        last_refreshed_at="now",
    )
    click.echo(render_page(enriched, entry))


@click.command()
@click.argument("file_key", default="hOV4QMBnDIG5s5OYkSrX9E")
@click.argument("page_node_id", default="7741:45837")
def main(file_key: str, page_node_id: str) -> None:
    """Fetch a Figma page, enrich with LLM, print rendered markdown."""
    asyncio.run(_run(file_key, page_node_id))


if __name__ == "__main__":
    main()
