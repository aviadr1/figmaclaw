"""figmaclaw image-urls — return Figma render URLs for frames without downloading.

Calls the Figma Image Export API and returns temporary S3 render URLs.
Perfect for embedding in markdown, GitHub discussions, or Slack.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from figmaclaw.commands._shared import require_figma_api_key
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.image_export import DEFAULT_IMAGE_BATCH_SIZE, get_image_urls_batched


@click.command("image-urls")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--nodes",
    default=None,
    help="Comma-separated node IDs to render. If omitted, uses all frames from frontmatter.",
)
@click.option(
    "--scale",
    default=0.5,
    type=click.FloatRange(min=0.01, max=4.0),
    show_default=True,
    help="Render scale (0.01–4).",
)
@click.option(
    "--format",
    "img_format",
    default="png",
    type=click.Choice(["png", "svg", "jpg", "pdf"], case_sensitive=False),
    show_default=True,
    help="Image format.",
)
@click.pass_context
def image_urls_cmd(
    ctx: click.Context,
    md_path: Path,
    nodes: str | None,
    scale: float,
    img_format: str,
) -> None:
    """Return Figma render URLs for frames in a figmaclaw .md file.

    Calls the Figma Image Export API and outputs JSON with temporary S3
    render URLs — no files are downloaded.

    Output: {"file_key": "...", "images": {"node_id": "url", ...}}
    """
    api_key = require_figma_api_key()

    repo_dir = Path(ctx.obj["repo_dir"])
    result = asyncio.run(_run(api_key, repo_dir, md_path, nodes, scale, img_format))
    click.echo(json.dumps(result, indent=2))


async def _run(
    api_key: str,
    repo_dir: Path,
    md_path: Path,
    nodes: str | None,
    scale: float,
    img_format: str,
) -> dict:
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(
            f"{md_path}: no figmaclaw frontmatter — is this a figmaclaw .md file?"
        )

    file_key = fm.file_key

    if nodes:
        node_ids = [n.strip() for n in nodes.split(",") if n.strip()]
    else:
        node_ids = list(fm.frames) if fm.frames else []

    if not node_ids:
        return {"file_key": file_key, "images": {}}

    async with FigmaClient(api_key) as client:
        all_urls = await get_image_urls_batched(
            client,
            file_key,
            node_ids,
            batch_size=DEFAULT_IMAGE_BATCH_SIZE,
            scale=scale,
            format=img_format,
        )

    return {"file_key": file_key, "images": all_urls}
