"""Tests for commands/image_urls.py.

INVARIANTS:
- image-urls returns JSON with file_key and an images dict
- When --nodes specified, only those nodes are requested
- When --nodes omitted, all frames from frontmatter are used
- Node IDs are batched in groups of 50 for the API call
- --scale and --format options are passed through to the API
- Missing FIGMA_API_KEY raises a UsageError
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from figmaclaw.commands import image_urls as image_urls_module
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import PageEntry
from figmaclaw.main import cli


def _make_page(node_ids: list[str] | None = None) -> FigmaPage:
    ids = node_ids or ["11:1", "11:2", "11:3"]
    frames = [FigmaFrame(node_id=nid, name=f"frame-{nid}", description="") for nid in ids]
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
    md = scaffold_page(page, entry)
    p = tmp_path / "page.md"
    p.write_text(md)
    return p


@pytest.mark.asyncio
async def test_image_urls_returns_urls_for_specified_nodes(tmp_path: Path) -> None:
    """INVARIANT: When --nodes specified, returns URLs for those nodes."""
    md_path = _write_md(tmp_path, _make_page())

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(
        return_value={
            "11:1": "https://s3.example.com/1.png",
            "11:2": "https://s3.example.com/2.png",
        }
    )

    with patch.object(image_urls_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await image_urls_module._run(
            "fake-key",
            tmp_path,
            md_path,
            nodes="11:1,11:2",
            scale=0.5,
            img_format="png",
        )

    assert result["file_key"] == "abc123"
    assert result["images"] == {
        "11:1": "https://s3.example.com/1.png",
        "11:2": "https://s3.example.com/2.png",
    }
    mock_client.get_image_urls.assert_called_once_with(
        "abc123",
        ["11:1", "11:2"],
        scale=0.5,
        format="png",
    )


@pytest.mark.asyncio
async def test_image_urls_uses_all_frames_when_no_nodes(tmp_path: Path) -> None:
    """INVARIANT: When --nodes omitted, all frames from frontmatter are requested."""
    md_path = _write_md(tmp_path, _make_page(["11:1", "11:2", "11:3"]))

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(
        return_value={
            "11:1": "https://s3.example.com/1.png",
            "11:2": "https://s3.example.com/2.png",
            "11:3": "https://s3.example.com/3.png",
        }
    )

    with patch.object(image_urls_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await image_urls_module._run(
            "fake-key",
            tmp_path,
            md_path,
            nodes=None,
            scale=0.5,
            img_format="png",
        )

    assert len(result["images"]) == 3
    # All three frame node IDs were requested
    called_ids = mock_client.get_image_urls.call_args[0][1]
    assert set(called_ids) == {"11:1", "11:2", "11:3"}


@pytest.mark.asyncio
async def test_image_urls_batches_over_50_nodes(tmp_path: Path) -> None:
    """INVARIANT: Node IDs are batched in groups of 50 for the API call."""
    node_ids = [f"1:{i}" for i in range(75)]
    md_path = _write_md(tmp_path, _make_page(node_ids))

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(
        return_value={nid: f"https://s3.example.com/{nid}.png" for nid in node_ids[:50]},
    )

    with patch.object(image_urls_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        await image_urls_module._run(
            "fake-key",
            tmp_path,
            md_path,
            nodes=None,
            scale=0.5,
            img_format="png",
        )

    # Two batches: 50 + 25
    assert mock_client.get_image_urls.call_count == 2
    first_batch = mock_client.get_image_urls.call_args_list[0][0][1]
    second_batch = mock_client.get_image_urls.call_args_list[1][0][1]
    assert len(first_batch) == 50
    assert len(second_batch) == 25


@pytest.mark.asyncio
async def test_image_urls_passes_scale_and_format(tmp_path: Path) -> None:
    """INVARIANT: --scale and --format options are passed through to the API."""
    md_path = _write_md(tmp_path, _make_page(["11:1"]))

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_image_urls = AsyncMock(
        return_value={
            "11:1": "https://s3.example.com/1.svg",
        }
    )

    with patch.object(image_urls_module, "FigmaClient") as MockClientClass:
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await image_urls_module._run(
            "fake-key",
            tmp_path,
            md_path,
            nodes="11:1",
            scale=2.0,
            img_format="svg",
        )

    mock_client.get_image_urls.assert_called_once_with(
        "abc123",
        ["11:1"],
        scale=2.0,
        format="svg",
    )
    assert result["images"]["11:1"] == "https://s3.example.com/1.svg"


def test_image_urls_missing_api_key(tmp_path: Path) -> None:
    """INVARIANT: Missing FIGMA_API_KEY raises a UsageError via the CLI."""
    md_path = _write_md(tmp_path, _make_page())

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--repo-dir", str(tmp_path), "image-urls", str(md_path)],
        env={"FIGMA_API_KEY": ""},
    )
    assert result.exit_code != 0
    assert "FIGMA_API_KEY" in result.output


@pytest.mark.asyncio
async def test_image_urls_empty_when_no_frames(tmp_path: Path) -> None:
    """INVARIANT: Returns empty images dict when frontmatter has no frames."""
    section = FigmaSection(node_id="10:1", name="empty", frames=[])
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

    result = await image_urls_module._run(
        "fake-key",
        tmp_path,
        md_path,
        nodes=None,
        scale=0.5,
        img_format="png",
    )

    assert result["file_key"] == "abc123"
    assert result["images"] == {}
