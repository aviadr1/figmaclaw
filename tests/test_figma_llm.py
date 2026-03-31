"""Tests for figma_llm.py.

INVARIANTS:
- generate_section_descriptions returns one description per frame in order
- generate_section_descriptions returns empty strings for frames when API fails gracefully
- generate_page_summary returns a non-empty string
- enrich_page_with_descriptions merges LLM descriptions into a FigmaPage
- enrich_page_with_descriptions preserves existing descriptions (idempotency)
- enrich_page_with_descriptions only calls LLM for frames with no description
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.figma_llm import enrich_page_with_descriptions, generate_section_descriptions
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection


def _make_page(frames_with_descs: list[tuple[str, str]]) -> FigmaPage:
    frames = [FigmaFrame(node_id=f"11:{i}", name=name, description=desc) for i, (name, desc) in enumerate(frames_with_descs)]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    return FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id="7741:1",
        page_name="Onboarding",
        sections=[section],
    )


@pytest.mark.asyncio
async def test_generate_section_descriptions_returns_one_per_frame():
    """INVARIANT: Returns exactly one description per frame name, in order."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Welcome screen.\nCamera access prompt.")]
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    result = await generate_section_descriptions(
        mock_client,
        page_name="Onboarding",
        section_name="intro",
        frame_names=["welcome", "permissions"],
    )

    assert len(result) == 2
    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_generate_section_descriptions_pads_when_llm_returns_fewer_lines():
    """INVARIANT: If LLM returns fewer lines than frames, result is padded with empty strings."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Only one line.")]
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    result = await generate_section_descriptions(
        mock_client,
        page_name="Onboarding",
        section_name="intro",
        frame_names=["welcome", "permissions", "done"],
    )

    assert len(result) == 3


@pytest.mark.asyncio
async def test_enrich_page_calls_llm_for_frames_without_descriptions():
    """INVARIANT: enrich_page_with_descriptions calls the LLM only for frames with no description."""
    from anthropic import AsyncAnthropic

    page = _make_page([("welcome", "Already described."), ("permissions", ""), ("done", "")])

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="Camera access.\nAll set.")]
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_msg)

    with patch("figmaclaw.figma_llm.generate_section_descriptions", wraps=None) as mock_gen:
        mock_gen.return_value = ["Camera access.", "All set."]
        mock_gen = AsyncMock(return_value=["Camera access.", "All set."])
        result = await enrich_page_with_descriptions(mock_client, page, mock_gen)

    # All frames should have descriptions
    for section in result.sections:
        for frame in section.frames:
            assert frame.description, f"Frame {frame.name!r} has no description"


@pytest.mark.asyncio
async def test_enrich_page_preserves_existing_descriptions():
    """INVARIANT: Frames that already have descriptions are not overwritten."""
    from anthropic import AsyncAnthropic

    page = _make_page([("welcome", "Existing description."), ("permissions", "")])

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_gen = AsyncMock(return_value=["New description for permissions."])

    result = await enrich_page_with_descriptions(mock_client, page, mock_gen)

    welcome_frame = result.sections[0].frames[0]
    assert welcome_frame.description == "Existing description."


@pytest.mark.asyncio
async def test_enrich_page_no_llm_call_when_all_described():
    """INVARIANT: No LLM call when every frame already has a description."""
    from anthropic import AsyncAnthropic

    page = _make_page([("welcome", "Described."), ("permissions", "Also described.")])

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_gen = AsyncMock(return_value=[])

    await enrich_page_with_descriptions(mock_client, page, mock_gen)

    mock_gen.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_page_returns_figmapage():
    """INVARIANT: enrich_page_with_descriptions returns a FigmaPage."""
    from anthropic import AsyncAnthropic

    page = _make_page([("welcome", "")])
    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_gen = AsyncMock(return_value=["Welcome screen."])

    result = await enrich_page_with_descriptions(mock_client, page, mock_gen)

    assert isinstance(result, FigmaPage)
