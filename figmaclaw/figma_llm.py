"""LLM-powered semantic enrichment for Figma pages.

Strategy: one call per page (not per section).

The LLM sees the entire page structure at once and returns:
  - frame descriptions (node_id-keyed, ≤20 words each)
  - page_summary (1-2 sentences covering the whole page's purpose)
  - inferred flow edges (list of [source_node_id, dest_node_id] pairs)

Flow edges supplement — not replace — prototype reactions from Figma.
The Mermaid diagram in the rendered .md is built from the union of both.

Output keyed by node_id so duplicate frame names across sections never collide.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from figmaclaw.figma_models import FigmaPage

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class PageEnrichment(BaseModel):
    """Structured LLM output for one Figma page."""

    frame_descriptions: dict[str, str] = Field(default_factory=dict)  # {node_id: description}
    page_summary: str = ""
    inferred_flows: list[tuple[str, str]] = Field(default_factory=list)


# Injectable: (client, page) -> PageEnrichment
GenerateFn = Callable[["AsyncAnthropic", FigmaPage], Awaitable[PageEnrichment]]


def _build_prompt(page: FigmaPage) -> str:
    """Build the full page-level enrichment prompt.

    Only includes frames that have no description yet.
    Returns empty string if nothing needs describing.
    """
    undescribed_by_section: list[tuple[str, list[tuple[str, str]]]] = []
    for section in page.sections:
        undescribed = [(f.node_id, f.name) for f in section.frames if not f.description]
        if undescribed:
            undescribed_by_section.append((section.name, undescribed))

    if not undescribed_by_section:
        return ""

    sections_block = ""
    for section_name, frames in undescribed_by_section:
        sections_block += f'Section: "{section_name}"\n'
        for node_id, name in frames:
            sections_block += f"  - {node_id}: {name}\n"

    flows_block = ""
    if page.flows:
        node_label: dict[str, str] = {
            frame.node_id: frame.name
            for section in page.sections
            for frame in section.frames
        }
        flow_lines = [
            f"  {src} ({node_label.get(src, src)}) → {dst} ({node_label.get(dst, dst)})"
            for src, dst in page.flows
        ]
        flows_block = "\nKnown prototype flows:\n" + "\n".join(flow_lines) + "\n"

    return (
        f"You are building a navigation index for a Figma design page.\n"
        f"An AI agent will use this to find the right screen, understand the flow,\n"
        f"and know what is unique about each state.\n\n"
        f"Page: {page.page_name}\n"
        f"File: {page.file_name}\n\n"
        f"Screens to describe (node_id: screen name):\n"
        f"{sections_block}"
        f"{flows_block}\n"
        f"Return JSON only, no other text:\n"
        f"{{\n"
        f'  "page_summary": "<1-2 sentences describing the whole page purpose and scope>",\n'
        f'  "frames": {{\n'
        f'    "<node_id>": "<≤20 word description: what it shows and what makes it distinct>",\n'
        f'    ...\n'
        f'  }},\n'
        f'  "flows": [\n'
        f'    ["<source_node_id>", "<dest_node_id>"],\n'
        f'    ...\n'
        f'  ]\n'
        f"}}"
    )


async def generate_page_enrichment(
    client: "AsyncAnthropic",
    page: FigmaPage,
    *,
    model: str | None = None,
) -> PageEnrichment:
    """Generate descriptions, page summary, and inferred flows for a page.

    Makes one LLM call for the entire page.
    Falls back gracefully on malformed JSON — never raises.
    """
    prompt = _build_prompt(page)
    if not prompt:
        return PageEnrichment(page_summary=page.page_summary)

    model = model or os.environ.get("LLM_MODEL", _DEFAULT_MODEL)

    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Extract JSON — model sometimes wraps in ```json ... ```
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        log.warning("LLM returned no JSON for page %r — using placeholders", page.page_name)
        return PageEnrichment()

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError as exc:
        log.warning("LLM JSON parse error for page %r: %s — using placeholders", page.page_name, exc)
        return PageEnrichment()

    frame_descriptions: dict[str, str] = {}
    raw_frames = data.get("frames", {})
    if isinstance(raw_frames, dict):
        frame_descriptions = {k: str(v) for k, v in raw_frames.items()}

    page_summary: str = str(data.get("page_summary", ""))

    inferred_flows: list[tuple[str, str]] = []
    raw_flows = data.get("flows", [])
    if isinstance(raw_flows, list):
        for edge in raw_flows:
            if isinstance(edge, list) and len(edge) == 2:
                inferred_flows.append((str(edge[0]), str(edge[1])))

    return PageEnrichment(
        frame_descriptions=frame_descriptions,
        page_summary=page_summary,
        inferred_flows=inferred_flows,
    )


# Convenience alias used by tests and pull_logic
async def generate_page_descriptions(
    client: "AsyncAnthropic",
    page: FigmaPage,
    *,
    model: str | None = None,
) -> dict[str, str]:
    """Return just the frame descriptions dict. Used by tests."""
    enrichment = await generate_page_enrichment(client, page, model=model)
    return enrichment.frame_descriptions


async def enrich_page_with_descriptions(
    client: "AsyncAnthropic",
    page: FigmaPage,
    *,
    generate_fn: GenerateFn | None = None,
) -> FigmaPage:
    """Return a copy of the page enriched with LLM descriptions, summary, and inferred flows.

    Frames that already have a description are never sent to the LLM (idempotency).
    Inferred flows are merged with existing prototype flows (deduped).
    generate_fn is injectable for testing; defaults to generate_page_enrichment.
    """
    has_undescribed = any(
        not frame.description
        for section in page.sections
        for frame in section.frames
    )
    if not has_undescribed:
        return page

    _generate_fn: GenerateFn = generate_fn if generate_fn is not None else generate_page_enrichment

    enrichment = await _generate_fn(client, page)

    # Merge frame descriptions (preserve existing, fill missing)
    new_sections = []
    for section in page.sections:
        new_frames = [
            frame.model_copy(update={
                "description": enrichment.frame_descriptions.get(frame.node_id, frame.description) or frame.description
            })
            for frame in section.frames
        ]
        new_sections.append(section.model_copy(update={"frames": new_frames}))

    # Merge flows: prototype reactions first, then inferred (deduped)
    existing_flow_set = set(page.flows)
    merged_flows = list(page.flows)
    for edge in enrichment.inferred_flows:
        if edge not in existing_flow_set:
            merged_flows.append(edge)
            existing_flow_set.add(edge)

    # Use LLM summary if we don't already have one
    summary = page.page_summary or enrichment.page_summary

    return page.model_copy(update={
        "sections": new_sections,
        "flows": merged_flows,
        "page_summary": summary,
    })
