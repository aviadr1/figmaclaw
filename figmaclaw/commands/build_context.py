"""figmaclaw build-context — generate use_figma calls to place a composite
"Usage in Context" frame next to a DS component set in the draft file.

The command reads section positions from the source page's frontmatter
(frame_sections field, written by the pull pass), fetches SVG or PNG data
for each section, and outputs a JSON array of use_figma call specs.

The caller (Claude Code / a skill) executes the calls in order.

Usage
-----
    figmaclaw build-context \\
        --source-md figma/community-in-live/pages/mobile-insights-tab-7423-8435.md \\
        --source-frame 7424:15980 \\
        --target-file FYtMg26IG7WkgxEcVdp86T \\
        --target-page 18:7 \\
        --comp-node 18:20 \\
        --comp-x 80 --comp-y 498 --comp-w 76 \\
        --label "Mobile Insights Tab — Community in Live"

Output
------
JSON array of call specs, one per use_figma call:

    [
      {"file_key": "...", "description": "...", "code": "..."},
      ...
    ]

Execute them in order with the use_figma MCP tool.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import click

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_frontmatter import SectionNode
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.in_context import fetch_section_data, make_context_calls


@click.command("build-context")
@click.option(
    "--source-md", "source_md", required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to the source product page .md file (contains frame_sections frontmatter).",
)
@click.option(
    "--source-frame", "source_frame_id", required=True,
    help="Node ID of the source frame to show in context (e.g. '7424:15980').",
)
@click.option(
    "--target-file", "target_file_key", required=True,
    help="Figma file key of the DS draft file.",
)
@click.option(
    "--target-page", "target_page_id", required=True,
    help="Node ID of the target page in the draft file (e.g. '18:7').",
)
@click.option(
    "--comp-node", "comp_node_id", required=True,
    help="Node ID of the component set (used in container name for uniqueness).",
)
@click.option("--comp-x", "comp_x", required=True, type=int,
              help="Canvas x of the component set.")
@click.option("--comp-y", "comp_y", required=True, type=int,
              help="Canvas y of the component set.")
@click.option("--comp-w", "comp_w", required=True, type=int,
              help="Width of the component set.")
@click.option("--label", "label", default="",
              help="Caption text placed below the context frame.")
@click.pass_context
def build_context_cmd(
    ctx: click.Context,
    source_md: Path,
    source_frame_id: str,
    target_file_key: str,
    target_page_id: str,
    comp_node_id: str,
    comp_x: int,
    comp_y: int,
    comp_w: int,
    label: str,
) -> None:
    """Generate use_figma calls to build a composite 'Usage in Context' frame.

    Reads section positions from source .md frontmatter (frame_sections field).
    Fetches SVG or PNG for each section via the Figma REST API.
    Outputs JSON array of use_figma call specs — execute in order.
    """
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if not api_key:
        raise click.UsageError("FIGMA_API_KEY environment variable is not set.")

    calls = asyncio.run(_run(
        api_key=api_key,
        source_md=source_md,
        source_frame_id=source_frame_id,
        target_file_key=target_file_key,
        target_page_id=target_page_id,
        comp_node_id=comp_node_id,
        comp_x=comp_x,
        comp_y=comp_y,
        comp_w=comp_w,
        label=label,
    ))
    click.echo(json.dumps(calls, indent=2))


async def _run(
    *,
    api_key: str,
    source_md: Path,
    source_frame_id: str,
    target_file_key: str,
    target_page_id: str,
    comp_node_id: str,
    comp_x: int,
    comp_y: int,
    comp_w: int,
    label: str,
) -> list[dict[str, str]]:
    # Read source page frontmatter to get section positions
    md_text = source_md.read_text()
    fm = parse_frontmatter(md_text)
    if fm is None:
        raise click.UsageError(
            f"{source_md}: no figmaclaw frontmatter — is this a figmaclaw .md file?"
        )

    sections = fm.frame_sections.get(source_frame_id)
    if not sections:
        raise click.UsageError(
            f"No frame_sections entry for frame {source_frame_id!r} in {source_md}.\n"
            f"Run 'figmaclaw pull' on the source file first to populate frame_sections."
        )

    source_file_key = fm.file_key

    # Infer source frame dimensions from section positions
    frame_w = max(s.x + s.w for s in sections) if sections else 0
    frame_h = max(s.y + s.h for s in sections) if sections else 0

    # Container name: stable, unique per (source frame, component set)
    safe_frame = source_frame_id.replace(":", "-")
    safe_comp = comp_node_id.replace(":", "-")
    container_name = f"ctx-{safe_frame}-{safe_comp}"

    # Fetch SVG or PNG for each section
    async with FigmaClient(api_key) as client:
        section_data_list = [
            await fetch_section_data(client, source_file_key, section)
            for section in sections
        ]

    return make_context_calls(
        target_file_key=target_file_key,
        target_page_id=target_page_id,
        container_name=container_name,
        frame_w=frame_w,
        frame_h=frame_h,
        comp_x=comp_x,
        comp_y=comp_y,
        comp_w=comp_w,
        label=label or _default_label(source_md, source_frame_id),
        section_data_list=section_data_list,
    )


def _default_label(source_md: Path, frame_id: str) -> str:
    """Generate a reasonable caption from the source .md filename."""
    stem = source_md.stem  # e.g. 'mobile-insights-tab-7423-8435'
    # Strip trailing node ID suffix (last two hyphen-separated tokens are the node ID)
    parts = stem.rsplit("-", 2)
    readable = parts[0].replace("-", " ").title() if len(parts) == 3 else stem
    return readable
