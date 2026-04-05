"""Tests for commands/sync.py.

INVARIANTS:
- sync fetches file meta and page node from Figma API
- sync updates only the frontmatter, preserving the LLM-authored body
- sync updates the manifest with the new page hash after re-sync
- sync fails with a usage error when FIGMA_API_KEY is not set
- sync fails with a usage error for a file with no figmaclaw frontmatter
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.commands import sync as sync_module
from figmaclaw.figma_api_models import FileMetaResponse
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_hash import compute_page_hash
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry


def _make_page() -> FigmaPage:
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description="Welcome screen."),
        FigmaFrame(node_id="11:2", name="permissions", description=""),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    return FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id="7741:45837",
        page_name="Onboarding",
        page_slug="onboarding",
        figma_url="https://www.figma.com/design/abc123?node-id=7741-45837",
        sections=[section],
        flows=[],
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
    )


def _make_entry(md_path: str = "figma/abc123/pages/onboarding.md") -> PageEntry:
    return PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path=md_path,
        page_hash="old-hash",
        last_refreshed_at="2026-03-31T00:00:00Z",
    )


def _fake_page_node() -> dict:
    return {
        "id": "7741:45837",
        "name": "Onboarding",
        "type": "CANVAS",
        "children": [
            {
                "id": "10:1",
                "name": "onboarding",
                "type": "SECTION",
                "children": [
                    {"id": "11:1", "name": "welcome", "type": "FRAME", "children": []},
                    {"id": "11:2", "name": "permissions", "type": "FRAME", "children": []},
                ],
            }
        ],
    }


def _fake_file_meta() -> "FileMetaResponse":
    return FileMetaResponse.model_validate({
        "name": "Web App",
        "version": "v2",
        "lastModified": "2026-03-31T12:00:00Z",
    })


@pytest.mark.asyncio
async def test_sync_updates_manifest_hash(tmp_path: Path) -> None:
    """INVARIANT: sync updates the manifest with the new page hash after re-syncing from Figma."""
    page = _make_page()
    entry = _make_entry("figma/abc123/pages/onboarding.md")
    md = scaffold_page(page, entry)
    md_path = tmp_path / "page.md"
    md_path.write_text(md)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.set_page_entry("abc123", "7741:45837", entry)
    state.save()

    page_node = _fake_page_node()
    expected_hash = compute_page_hash(page_node)

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta())
    mock_client.get_page = AsyncMock(return_value=page_node)

    with patch.object(sync_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        await sync_module._run("fake-api-key", tmp_path, md_path, auto_commit=False)

    state2 = FigmaSyncState(tmp_path)
    state2.load()
    page_entry = state2.manifest.files["abc123"].pages["7741:45837"]
    assert page_entry.page_hash == expected_hash


@pytest.mark.asyncio
async def test_sync_frontmatter_has_frame_ids(tmp_path: Path) -> None:
    """INVARIANT: sync puts frame IDs (not descriptions) in frontmatter."""
    page = _make_page()
    entry = _make_entry("figma/abc123/pages/onboarding.md")
    md = scaffold_page(page, entry)
    md_path = tmp_path / "page.md"
    md_path.write_text(md)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.set_page_entry("abc123", "7741:45837", entry)
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta())
    mock_client.get_page = AsyncMock(return_value=_fake_page_node())

    with patch.object(sync_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        await sync_module._run("fake-api-key", tmp_path, md_path, auto_commit=False)

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert isinstance(fm.frames, list)
    assert "11:1" in fm.frames
    assert "11:2" in fm.frames


@pytest.mark.asyncio
async def test_sync_preserves_body(tmp_path: Path) -> None:
    """INVARIANT: sync updates only frontmatter — the LLM-authored body is never overwritten."""
    page = _make_page()
    entry = _make_entry("figma/abc123/pages/onboarding.md")
    md = scaffold_page(page, entry)

    # Simulate LLM-authored body by replacing placeholders with real prose
    md = md.replace(
        "<!-- LLM: Write a 2-3 sentence page summary describing what this page covers -->",
        "This page covers the onboarding flow with welcome and permissions screens.",
    )
    md = md.replace(
        "<!-- LLM: Write a 1-sentence section intro if the section has a distinct theme -->",
        "The onboarding section walks new users through initial setup.",
    )
    md_path = tmp_path / "page.md"
    md_path.write_text(md)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.set_page_entry("abc123", "7741:45837", entry)
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta())
    mock_client.get_page = AsyncMock(return_value=_fake_page_node())

    with patch.object(sync_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        await sync_module._run("fake-api-key", tmp_path, md_path, auto_commit=False)

    updated = md_path.read_text()
    # Body prose must be preserved
    assert "This page covers the onboarding flow with welcome and permissions screens." in updated
    assert "The onboarding section walks new users through initial setup." in updated
    # Frontmatter must still be valid
    fm = parse_frontmatter(updated)
    assert fm is not None


@pytest.mark.asyncio
async def test_sync_fails_for_non_figmaclaw_file(tmp_path: Path) -> None:
    """INVARIANT: sync raises UsageError when the .md file has no figmaclaw frontmatter."""
    import click

    md_path = tmp_path / "plain.md"
    md_path.write_text("# Plain markdown\n\nNo frontmatter.\n")

    with pytest.raises(click.UsageError):
        await sync_module._run("fake-api-key", tmp_path, md_path, auto_commit=False)
