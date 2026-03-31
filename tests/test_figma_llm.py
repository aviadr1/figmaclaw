"""Tests for figma_llm.py.

INVARIANTS:
- generate_page_descriptions makes one LLM call per page (not per section)
- generate_page_descriptions returns {node_id: description} keyed by node_id
- generate_page_descriptions handles duplicate frame names across sections
- generate_page_descriptions falls back gracefully when JSON is malformed
- enrich_page_with_descriptions merges LLM descriptions into a FigmaPage by node_id
- enrich_page_with_descriptions preserves existing descriptions (idempotency)
- enrich_page_with_descriptions only calls LLM when undescribed frames exist
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from figmaclaw.figma_llm import PageEnrichment, enrich_page_with_descriptions, generate_page_descriptions
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection


def _make_page(frames_with_descs: list[tuple[str, str, str]]) -> FigmaPage:
    """frames_with_descs: list of (node_id, name, description)."""
    frames = [FigmaFrame(node_id=nid, name=name, description=desc) for nid, name, desc in frames_with_descs]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    return FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id="7741:1",
        page_name="Onboarding",
        sections=[section],
    )


def _make_multi_section_page() -> FigmaPage:
    """Page with two sections where the same frame name appears in both."""
    s1 = FigmaSection(node_id="10:1", name="schedule event", frames=[
        FigmaFrame(node_id="11:1", name="information box", description=""),
        FigmaFrame(node_id="11:2", name="socials enabled", description=""),
    ])
    s2 = FigmaSection(node_id="10:2", name="override settings", frames=[
        FigmaFrame(node_id="11:3", name="information box", description=""),  # same name!
    ])
    return FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id="7741:1",
        page_name="Scheduling",
        sections=[s1, s2],
    )


def _mock_llm_response(json_text: str) -> MagicMock:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=json_text)]
    return mock_msg


# --- generate_page_descriptions ---

@pytest.mark.asyncio
async def test_generate_page_descriptions_makes_one_call():
    """INVARIANT: One LLM call per page regardless of section count."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(
        '{"frames": {"11:1": "Welcome screen.", "11:2": "Permissions prompt."}}'
    ))

    page = _make_page([("11:1", "welcome", ""), ("11:2", "permissions", "")])
    await generate_page_descriptions(mock_client, page)

    mock_client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_generate_page_descriptions_returns_node_id_keyed_dict():
    """INVARIANT: Returns {node_id: description} so duplicate frame names don't collide."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(
        '{"frames": {"11:1": "Welcome screen.", "11:2": "Camera access prompt."}}'
    ))

    page = _make_page([("11:1", "welcome", ""), ("11:2", "permissions", "")])
    result = await generate_page_descriptions(mock_client, page)

    assert result["11:1"] == "Welcome screen."
    assert result["11:2"] == "Camera access prompt."


@pytest.mark.asyncio
async def test_generate_page_descriptions_handles_duplicate_frame_names():
    """INVARIANT: Frames with same name in different sections get distinct descriptions via node_id."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(
        '{"frames": {"11:1": "Scheduling info box.", "11:2": "Socials toggle.", "11:3": "Override info box."}}'
    ))

    page = _make_multi_section_page()
    result = await generate_page_descriptions(mock_client, page)

    assert result["11:1"] == "Scheduling info box."
    assert result["11:3"] == "Override info box."
    # Different descriptions for same frame name in different sections
    assert result["11:1"] != result["11:3"]


@pytest.mark.asyncio
async def test_generate_page_descriptions_falls_back_on_malformed_json():
    """INVARIANT: Malformed JSON response returns empty dict (no exception)."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(
        "Sorry, I cannot generate descriptions for this page."
    ))

    page = _make_page([("11:1", "welcome", "")])
    result = await generate_page_descriptions(mock_client, page)

    assert result == {}


@pytest.mark.asyncio
async def test_generate_page_descriptions_skips_already_described_frames():
    """INVARIANT: Frames with existing descriptions are excluded from the LLM prompt."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(
        '{"frames": {"11:2": "Camera access prompt."}}'
    ))

    # 11:1 already has a description; only 11:2 is undescribed
    page = _make_page([("11:1", "welcome", "Already described."), ("11:2", "permissions", "")])
    await generate_page_descriptions(mock_client, page)

    call_args = mock_client.messages.create.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"]
    assert "11:2" in prompt_text
    assert "11:1" not in prompt_text  # already described, excluded


@pytest.mark.asyncio
async def test_generate_page_descriptions_returns_empty_when_all_described():
    """INVARIANT: No LLM call and empty dict when all frames already have descriptions."""
    from anthropic import AsyncAnthropic

    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock()

    page = _make_page([("11:1", "welcome", "Already there."), ("11:2", "permissions", "Also there.")])
    result = await generate_page_descriptions(mock_client, page)

    mock_client.messages.create.assert_not_called()
    assert result == {}


# --- enrich_page_with_descriptions ---

@pytest.mark.asyncio
async def test_enrich_page_fills_missing_descriptions():
    """INVARIANT: enrich_page_with_descriptions fills undescribed frames from LLM result."""
    from anthropic import AsyncAnthropic

    page = _make_page([("11:1", "welcome", ""), ("11:2", "permissions", "")])
    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_generate = AsyncMock(return_value=PageEnrichment(
        frame_descriptions={"11:1": "Welcome screen.", "11:2": "Camera access."}
    ))

    result = await enrich_page_with_descriptions(mock_client, page, generate_fn=mock_generate)

    frames = result.sections[0].frames
    assert frames[0].description == "Welcome screen."
    assert frames[1].description == "Camera access."


@pytest.mark.asyncio
async def test_enrich_page_preserves_existing_descriptions():
    """INVARIANT: Frames that already have descriptions are not overwritten."""
    from anthropic import AsyncAnthropic

    page = _make_page([("11:1", "welcome", "Existing description."), ("11:2", "permissions", "")])
    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_generate = AsyncMock(return_value=PageEnrichment(
        frame_descriptions={"11:2": "Camera access."}
    ))

    result = await enrich_page_with_descriptions(mock_client, page, generate_fn=mock_generate)

    assert result.sections[0].frames[0].description == "Existing description."


@pytest.mark.asyncio
async def test_enrich_page_no_llm_call_when_all_described():
    """INVARIANT: No generate_fn call when every frame already has a description."""
    from anthropic import AsyncAnthropic

    page = _make_page([("11:1", "welcome", "Described."), ("11:2", "permissions", "Also described.")])
    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_generate = AsyncMock(return_value={})

    await enrich_page_with_descriptions(mock_client, page, generate_fn=mock_generate)

    mock_generate.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_page_returns_figmapage():
    """INVARIANT: enrich_page_with_descriptions returns a FigmaPage."""
    from anthropic import AsyncAnthropic

    page = _make_page([("11:1", "welcome", "")])
    mock_client = MagicMock(spec=AsyncAnthropic)
    mock_generate = AsyncMock(return_value=PageEnrichment(
        frame_descriptions={"11:1": "Welcome screen."}
    ))

    result = await enrich_page_with_descriptions(mock_client, page, generate_fn=mock_generate)

    assert isinstance(result, FigmaPage)
