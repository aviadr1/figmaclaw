"""LLM-powered semantic description generation for Figma frames.

Strategy:
- One LLM call per section (not per frame) — batch all frames together
- Only call the LLM for frames that have no description (idempotency)
- enrich_page_with_descriptions() merges LLM output back into the FigmaPage

The generate_fn parameter on enrich_page_with_descriptions() makes it
easy to test without real Anthropic calls — pass a mock async callable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from figmaclaw.figma_models import FigmaPage, FigmaSection

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

GenerateFn = Callable[[str, str, list[str]], Awaitable[list[str]]]


async def generate_section_descriptions(
    client: "AsyncAnthropic",
    *,
    page_name: str,
    section_name: str,
    frame_names: list[str],
    model: str | None = None,
) -> list[str]:
    """Generate one-sentence descriptions for a batch of frames in a section.

    Returns a list of descriptions in the same order as frame_names.
    If the LLM returns fewer lines than frames, the result is padded with "".
    """
    if not frame_names:
        return []

    model = model or os.environ.get("LLM_MODEL", _DEFAULT_MODEL)
    numbered = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(frame_names))
    prompt = (
        f"You are describing screens in a Figma design file for an AI navigation index.\n"
        f"Page: {page_name}\n"
        f"Section: {section_name}\n\n"
        f"For each screen below, write exactly one concise sentence (max 20 words) describing "
        f"what the screen shows and its purpose. Output one sentence per line, in the same order.\n\n"
        f"{numbered}"
    )

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]

    # Strip leading "1. " / "2. " numbering if the LLM added it
    cleaned: list[str] = []
    for line in lines:
        if line and line[0].isdigit() and ". " in line[:4]:
            line = line.split(". ", 1)[1]
        cleaned.append(line)

    # Pad or trim to match the number of frames
    cleaned.extend([""] * max(0, len(frame_names) - len(cleaned)))
    return cleaned[: len(frame_names)]


async def enrich_page_with_descriptions(
    client: "AsyncAnthropic",
    page: FigmaPage,
    generate_fn: GenerateFn | None = None,
) -> FigmaPage:
    """Return a copy of page with LLM-generated descriptions for undescribed frames.

    Frames that already have a description are not touched (idempotency).
    generate_fn is injectable for testing; defaults to generate_section_descriptions.
    """
    if generate_fn is None:
        async def _default_generate(page_name: str, section_name: str, names: list[str]) -> list[str]:
            return await generate_section_descriptions(
                client, page_name=page_name, section_name=section_name, frame_names=names
            )
        generate_fn = _default_generate

    new_sections: list[FigmaSection] = []
    for section in page.sections:
        undescribed = [f for f in section.frames if not f.description]
        if not undescribed:
            new_sections.append(section)
            continue

        log.info(
            "Generating descriptions for %d frame(s) in section %r",
            len(undescribed),
            section.name,
        )
        new_descs = await generate_fn(page.page_name, section.name, [f.name for f in undescribed])
        desc_map = dict(zip([f.name for f in undescribed], new_descs))

        new_frames = [
            frame.model_copy(update={"description": desc_map.get(frame.name, frame.description) or frame.description})
            for frame in section.frames
        ]
        new_sections.append(section.model_copy(update={"frames": new_frames}))

    return page.model_copy(update={"sections": new_sections})
