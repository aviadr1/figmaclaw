"""Tests for commands/screenshots.py.

INVARIANTS:
- screenshots returns a manifest with file_key and a screenshots list
- Each successful download appears in the manifest with node_id and local path
- A failed download does not abort the batch — other successful downloads are returned
- screenshots returns an empty list when there are no frames in the .md file
- screenshots with --pending-only downloads only frames without descriptions
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.commands import screenshots as screenshots_module
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import render_page
from figmaclaw.figma_sync_state import PageEntry


def _make_page(node_ids: list[str] | None = None, described: bool = False) -> FigmaPage:
    ids = node_ids or ["11:1", "11:2", "11:3"]
    frames = [
        FigmaFrame(
            node_id=nid,
            name=f"frame-{nid}",
            description=f"Desc {nid}" if described else "",
        )
        for nid in ids
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


def _write_md(tmp_path: Path, page: FigmaPage) -> Path:
    entry = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash="deadbeef",
        last_refreshed_at="2026-03-31T00:00:00Z",
    )
    md = render_page(page, entry)
    p = tmp_path / "page.md"
    p.write_text(md)
    return p


@pytest.mark.asyncio
async def test_screenshots_returns_manifest_with_file_key(tmp_path: Path) -> None:
    """INVARIANT: screenshots result always contains file_key."""
    md_path = _write_md(tmp_path, _make_page())

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(return_value={
        "11:1": "http://example.com/1.png",
        "11:2": "http://example.com/2.png",
        "11:3": "http://example.com/3.png",
    })
    mock_client.download_url = AsyncMock(return_value=b"\x89PNG\r\n")

    with patch.object(screenshots_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await screenshots_module._run("fake-key", tmp_path, md_path, pending_only=False)

    assert result["file_key"] == "abc123"
    assert "screenshots" in result


@pytest.mark.asyncio
async def test_screenshots_successful_downloads_in_manifest(tmp_path: Path) -> None:
    """INVARIANT: Each successfully downloaded frame appears in the screenshots list with node_id and path."""
    md_path = _write_md(tmp_path, _make_page(["11:1", "11:2"]))

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(return_value={
        "11:1": "http://example.com/1.png",
        "11:2": "http://example.com/2.png",
    })
    mock_client.download_url = AsyncMock(return_value=b"\x89PNG\r\n")

    with patch.object(screenshots_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await screenshots_module._run("fake-key", tmp_path, md_path, pending_only=False)

    node_ids = {s["node_id"] for s in result["screenshots"]}
    assert node_ids == {"11:1", "11:2"}
    for entry in result["screenshots"]:
        assert "path" in entry
        assert entry["path"].endswith(".png")


@pytest.mark.asyncio
async def test_screenshots_failed_download_does_not_abort_batch(tmp_path: Path) -> None:
    """INVARIANT: A failed download is excluded from the result without aborting other downloads."""
    md_path = _write_md(tmp_path, _make_page(["11:1", "11:2", "11:3"]))

    call_count = 0

    async def download_side_effect(url: str) -> bytes:
        nonlocal call_count
        call_count += 1
        if "2" in url:
            raise OSError("connection refused")
        return b"\x89PNG\r\n"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(return_value={
        "11:1": "http://example.com/1.png",
        "11:2": "http://example.com/2.png",
        "11:3": "http://example.com/3.png",
    })
    mock_client.download_url = AsyncMock(side_effect=download_side_effect)

    with patch.object(screenshots_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await screenshots_module._run("fake-key", tmp_path, md_path, pending_only=False)

    # All 3 downloads were attempted
    assert call_count == 3
    # Only the 2 successful ones are in the result
    node_ids = {s["node_id"] for s in result["screenshots"]}
    assert "11:1" in node_ids
    assert "11:3" in node_ids
    assert "11:2" not in node_ids


@pytest.mark.asyncio
async def test_screenshots_empty_when_no_frames(tmp_path: Path) -> None:
    """INVARIANT: screenshots returns an empty list when the .md has no frames."""
    frames_section = FigmaSection(node_id="10:1", name="empty", frames=[])
    page = FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id="7741:45837",
        page_name="Onboarding",
        page_slug="onboarding",
        figma_url="https://www.figma.com/design/abc123?node-id=7741-45837",
        sections=[frames_section],
        flows=[],
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
    )
    md_path = _write_md(tmp_path, page)

    mock_client = MagicMock(spec=FigmaClient)

    with patch.object(screenshots_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await screenshots_module._run("fake-key", tmp_path, md_path, pending_only=False)

    assert result["screenshots"] == []


@pytest.mark.asyncio
async def test_screenshots_pending_only_skips_described_frames(tmp_path: Path) -> None:
    """INVARIANT: --pending-only downloads only frames whose frontmatter description is empty."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description="Already described."),
        FigmaFrame(node_id="11:2", name="permissions", description=""),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = FigmaPage(
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
    md_path = _write_md(tmp_path, page)

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(return_value={"11:2": "http://example.com/2.png"})
    mock_client.download_url = AsyncMock(return_value=b"\x89PNG\r\n")

    with patch.object(screenshots_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await screenshots_module._run("fake-key", tmp_path, md_path, pending_only=True)

    # Only the undescribed frame was requested
    mock_client.get_image_urls.assert_called_once_with("abc123", ["11:2"])
    node_ids = {s["node_id"] for s in result["screenshots"]}
    assert node_ids == {"11:2"}


def test_screenshots_semaphore_limit_constant() -> None:
    """INVARIANT: The max concurrent downloads constant is set to a sensible limit (<= 20)."""
    assert screenshots_module._MAX_CONCURRENT_DOWNLOADS <= 20
    assert screenshots_module._MAX_CONCURRENT_DOWNLOADS >= 1
