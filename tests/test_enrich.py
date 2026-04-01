"""Tests for commands/enrich.py.

INVARIANTS:
- enrich fetches file meta and page node from Figma API
- enrich writes the updated .md file, preserving existing descriptions
- enrich updates the manifest with the new page hash after re-sync
- enrich fails with a usage error when FIGMA_API_KEY is not set
- enrich fails with a usage error for a file with no figmaclaw frontmatter
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.commands import enrich as enrich_module
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_hash import compute_page_hash
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import render_page
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


def _fake_file_meta() -> dict:
    return {
        "name": "Web App",
        "version": "v2",
        "lastModified": "2026-03-31T12:00:00Z",
    }


@pytest.mark.asyncio
async def test_enrich_updates_manifest_hash(tmp_path: Path) -> None:
    """INVARIANT: enrich updates the manifest with the new page hash after re-syncing from Figma."""
    page = _make_page()
    entry = _make_entry("figma/abc123/pages/onboarding.md")
    md = render_page(page, entry)
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

    with patch.object(enrich_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        await enrich_module._run("fake-api-key", tmp_path, md_path, auto_commit=False)

    state2 = FigmaSyncState(tmp_path)
    state2.load()
    page_entry = state2.manifest.files["abc123"].pages["7741:45837"]
    assert page_entry.page_hash == expected_hash


@pytest.mark.asyncio
async def test_enrich_preserves_existing_descriptions(tmp_path: Path) -> None:
    """INVARIANT: enrich restores existing frame descriptions into the re-synced .md file."""
    page = _make_page()  # has "Welcome screen." on 11:1
    entry = _make_entry("figma/abc123/pages/onboarding.md")
    md = render_page(page, entry)
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

    with patch.object(enrich_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        await enrich_module._run("fake-api-key", tmp_path, md_path, auto_commit=False)

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert fm.frames.get("11:1") == "Welcome screen."


@pytest.mark.asyncio
async def test_enrich_fails_for_non_figmaclaw_file(tmp_path: Path) -> None:
    """INVARIANT: enrich raises UsageError when the .md file has no figmaclaw frontmatter."""
    import click

    md_path = tmp_path / "plain.md"
    md_path.write_text("# Plain markdown\n\nNo frontmatter.\n")

    with pytest.raises(click.UsageError):
        await enrich_module._run("fake-api-key", tmp_path, md_path, auto_commit=False)
