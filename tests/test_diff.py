"""Tests for commands/diff.py — figmaclaw diff (Figma API-based).

Tests mock the FigmaClient to verify that the diff command correctly
compares Figma file versions and detects structural design changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.commands import diff as diff_module
from figmaclaw.commands.diff import (
    FileDiff,
    FrameChange,
    PageDiff,
    VersionInfo,
    _extract_frames,
    _find_version_before,
    _run,
)
from figmaclaw.figma_api_models import FileMetaResponse, VersionSummary, VersionUser
from figmaclaw.figma_client import FigmaClient


def _vs(*, id: str, created_at: str, label: str = "", handle: str = "") -> VersionSummary:
    """Test helper: build a VersionSummary from keyword args."""
    return VersionSummary(
        id=id, created_at=created_at, label=label,
        user=VersionUser(handle=handle),
    )

# ── Fixtures ────────────────────────────────────────────────────────

_PAGE_MD = """\
---
file_key: abc123
page_node_id: '100:1'
frames: ['11:1', '11:2']
flows: []
---

# Test Page
"""

_PAGE_MD_2 = """\
---
file_key: abc123
page_node_id: '200:1'
frames: ['21:1']
flows: []
---

# Page 2
"""

_PAGE_MD_OTHER_FILE = """\
---
file_key: xyz789
page_node_id: '300:1'
frames: ['31:1']
flows: []
---

# Other File Page
"""


def _make_canvas(page_id: str, frames: list[dict]) -> dict:
    """Build a minimal CANVAS node dict for from_page_node()."""
    return {
        "id": page_id,
        "name": f"Page {page_id}",
        "type": "CANVAS",
        "children": frames,
    }


def _make_frame(node_id: str, name: str, reactions: list | None = None) -> dict:
    return {
        "id": node_id,
        "name": name,
        "type": "FRAME",
        "visible": True,
        "children": [],
        "reactions": reactions or [],
    }


# ── Unit tests: _extract_frames ─────────────────────────────────────


def test_extract_frames_from_canvas() -> None:
    """Frames are extracted from a CANVAS node."""
    canvas = _make_canvas("100:1", [
        _make_frame("11:1", "Welcome"),
        _make_frame("11:2", "Login"),
    ])
    frames, flows = _extract_frames(canvas, "abc123")
    assert set(frames.keys()) == {"11:1", "11:2"}
    assert frames["11:1"].name == "Welcome"
    assert flows == []


def test_extract_frames_empty_node() -> None:
    """Empty node returns empty results."""
    frames, flows = _extract_frames({}, "abc123")
    assert frames == {}
    assert flows == []


def test_extract_flows() -> None:
    """Prototype NAVIGATE reactions produce flow edges."""
    canvas = _make_canvas("100:1", [
        _make_frame("11:1", "A", reactions=[
            {"action": {"destinationId": "11:2", "navigation": "NAVIGATE"}},
        ]),
        _make_frame("11:2", "B"),
    ])
    frames, flows = _extract_frames(canvas, "abc123")
    assert ("11:1", "11:2") in flows


# ── Unit tests: _find_version_before ────────────────────────────────


@pytest.mark.asyncio
async def test_find_version_before_splits_correctly() -> None:
    """Versions before cutoff go to old_version, after go to in_range."""
    from datetime import datetime, timezone

    client = MagicMock(spec=FigmaClient)
    client.get_versions = AsyncMock(return_value=[
        _vs(id="v3", created_at="2026-04-03T12:00:00Z", handle="bart"),
        _vs(id="v2", created_at="2026-04-01T12:00:00Z", label="milestone", handle="bart"),
        _vs(id="v1", created_at="2026-03-25T12:00:00Z", handle="jakub"),
    ])

    cutoff = datetime(2026, 3, 28, tzinfo=timezone.utc)
    old, in_range = await _find_version_before(client, "abc123", cutoff)

    assert old is not None
    assert old.id == "v1"
    assert old.user == "jakub"
    assert len(in_range) == 2
    assert in_range[0].id == "v2"  # oldest first
    assert in_range[1].id == "v3"


@pytest.mark.asyncio
async def test_find_version_no_old_version() -> None:
    """If all versions are in the window, old_version is None."""
    from datetime import datetime, timezone

    client = MagicMock(spec=FigmaClient)
    client.get_versions = AsyncMock(return_value=[
        _vs(id="v2", created_at="2026-04-02T12:00:00Z", handle="bart"),
        _vs(id="v1", created_at="2026-04-01T12:00:00Z", handle="bart"),
    ])

    cutoff = datetime(2026, 3, 28, tzinfo=timezone.utc)
    old, in_range = await _find_version_before(client, "abc123", cutoff)

    assert old is None
    assert len(in_range) == 2


# ── Integration tests: _run ─────────────────────────────────────────


def _recent_meta() -> FileMetaResponse:
    """Return a FileMetaResponse with a recent lastModified date."""
    return FileMetaResponse(
        name="Test File", version="v2", lastModified="2026-04-03T12:00:00Z",
    )


def _mock_get_file_shallow(old_canvases: list[dict], new_canvases: list[dict]):
    """Build a mock get_file_shallow side-effect from old/new canvas lists."""
    def _make_tree(canvases: list[dict]) -> dict:
        return {
            "name": "Test File",
            "version": "v2",
            "lastModified": "2026-04-03T12:00:00Z",
            "document": {"children": canvases},
        }

    async def _get_file_shallow(fk, *, version=None):
        if version:
            return _make_tree(old_canvases)
        return _make_tree(new_canvases)
    return _get_file_shallow


@pytest.mark.asyncio
async def test_run_detects_added_frames(tmp_path: Path) -> None:
    """Frames added between old version and current should be reported."""
    # Set up tracked .md file
    figma_dir = tmp_path / "figma" / "app"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-100-1.md").write_text(_PAGE_MD)

    old_canvas = _make_canvas("100:1", [
        _make_frame("11:1", "Welcome"),
        _make_frame("11:2", "Login"),
    ])
    new_canvas = _make_canvas("100:1", [
        _make_frame("11:1", "Welcome"),
        _make_frame("11:2", "Login"),
        _make_frame("11:3", "Dashboard"),
    ])

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_versions = AsyncMock(return_value=[
        _vs(id="v2", created_at="2026-04-03T12:00:00Z", handle="bart"),
        _vs(id="v1", created_at="2026-03-25T12:00:00Z", handle="bart"),
    ])
    mock_client.get_file_meta = AsyncMock(return_value=_recent_meta())
    mock_client.get_file_shallow = AsyncMock(
        side_effect=_mock_get_file_shallow([old_canvas], [new_canvas]),
    )

    with patch.object(diff_module, "FigmaClient") as MockCls:
        MockCls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockCls.return_value.__aexit__ = AsyncMock(return_value=False)

        results, _, _ = await _run("fake-key", tmp_path / "figma", "7d")

    assert len(results) == 1
    fd = results[0]
    assert fd.file_key == "abc123"
    assert len(fd.pages) == 1
    p = fd.pages[0]
    assert len(p.added_frames) == 1
    assert p.added_frames[0].node_id == "11:3"
    assert p.added_frames[0].name == "Dashboard"
    assert p.frames_before == 2
    assert p.frames_after == 3


@pytest.mark.asyncio
async def test_run_detects_removed_frames(tmp_path: Path) -> None:
    """Frames removed between versions should be reported."""
    figma_dir = tmp_path / "figma" / "app"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-100-1.md").write_text(_PAGE_MD)

    old_canvas = _make_canvas("100:1", [
        _make_frame("11:1", "Welcome"),
        _make_frame("11:2", "Login"),
    ])
    new_canvas = _make_canvas("100:1", [
        _make_frame("11:1", "Welcome"),
    ])

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_versions = AsyncMock(return_value=[
        _vs(id="v2", created_at="2026-04-03T12:00:00Z", handle="bart"),
        _vs(id="v1", created_at="2026-03-25T12:00:00Z", handle="bart"),
    ])
    mock_client.get_file_meta = AsyncMock(return_value=_recent_meta())
    mock_client.get_file_shallow = AsyncMock(
        side_effect=_mock_get_file_shallow([old_canvas], [new_canvas]),
    )

    with patch.object(diff_module, "FigmaClient") as MockCls:
        MockCls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockCls.return_value.__aexit__ = AsyncMock(return_value=False)

        results, _, _ = await _run("fake-key", tmp_path / "figma", "7d")

    p = results[0].pages[0]
    assert len(p.removed_frames) == 1
    assert p.removed_frames[0].node_id == "11:2"


@pytest.mark.asyncio
async def test_run_detects_renames(tmp_path: Path) -> None:
    """Frame renames should be detected."""
    figma_dir = tmp_path / "figma" / "app"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-100-1.md").write_text(_PAGE_MD)

    old_canvas = _make_canvas("100:1", [
        _make_frame("11:1", "Welcome"),
    ])
    new_canvas = _make_canvas("100:1", [
        _make_frame("11:1", "Onboarding"),
    ])

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_versions = AsyncMock(return_value=[
        _vs(id="v2", created_at="2026-04-03T12:00:00Z"),
        _vs(id="v1", created_at="2026-03-25T12:00:00Z"),
    ])
    mock_client.get_file_meta = AsyncMock(return_value=_recent_meta())
    mock_client.get_file_shallow = AsyncMock(
        side_effect=_mock_get_file_shallow([old_canvas], [new_canvas]),
    )

    with patch.object(diff_module, "FigmaClient") as MockCls:
        MockCls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockCls.return_value.__aexit__ = AsyncMock(return_value=False)

        results, _, _ = await _run("fake-key", tmp_path / "figma", "7d")

    p = results[0].pages[0]
    assert len(p.renamed_frames) == 1
    assert p.renamed_frames[0].old_name == "Welcome"
    assert p.renamed_frames[0].new_name == "Onboarding"


@pytest.mark.asyncio
async def test_run_no_changes_skips_file(tmp_path: Path) -> None:
    """Files with no changes in the window should not appear."""
    figma_dir = tmp_path / "figma" / "app"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-100-1.md").write_text(_PAGE_MD)

    # No versions in range
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_recent_meta())
    mock_client.get_versions = AsyncMock(return_value=[
        _vs(id="v1", created_at="2026-03-20T12:00:00Z"),
    ])

    with patch.object(diff_module, "FigmaClient") as MockCls:
        MockCls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockCls.return_value.__aexit__ = AsyncMock(return_value=False)

        results, _, _ = await _run("fake-key", tmp_path / "figma", "7d")

    assert results == []


@pytest.mark.asyncio
async def test_run_multiple_pages_in_file(tmp_path: Path) -> None:
    """Multiple pages in the same file are compared."""
    figma_dir = tmp_path / "figma" / "app"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-100-1.md").write_text(_PAGE_MD)
    (figma_dir / "page-200-1.md").write_text(_PAGE_MD_2)

    canvas_100 = _make_canvas("100:1", [_make_frame("11:1", "A"), _make_frame("11:2", "B")])
    canvas_200 = _make_canvas("200:1", [_make_frame("21:1", "X"), _make_frame("21:2", "Y")])
    old_canvas_100 = _make_canvas("100:1", [_make_frame("11:1", "A")])
    old_canvas_200 = _make_canvas("200:1", [_make_frame("21:1", "X")])

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_versions = AsyncMock(return_value=[
        _vs(id="v2", created_at="2026-04-03T12:00:00Z", handle="bart"),
        _vs(id="v1", created_at="2026-03-25T12:00:00Z"),
    ])
    mock_client.get_file_meta = AsyncMock(return_value=_recent_meta())
    mock_client.get_file_shallow = AsyncMock(
        side_effect=_mock_get_file_shallow(
            [old_canvas_100, old_canvas_200],
            [canvas_100, canvas_200],
        ),
    )

    with patch.object(diff_module, "FigmaClient") as MockCls:
        MockCls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockCls.return_value.__aexit__ = AsyncMock(return_value=False)

        results, _, _ = await _run("fake-key", tmp_path / "figma", "7d")

    assert len(results) == 1
    assert len(results[0].pages) == 2


@pytest.mark.asyncio
async def test_run_version_users_tracked(tmp_path: Path) -> None:
    """Version authors should be captured."""
    figma_dir = tmp_path / "figma" / "app"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-100-1.md").write_text(_PAGE_MD)

    canvas = _make_canvas("100:1", [_make_frame("11:1", "A"), _make_frame("11:3", "New")])
    old_canvas = _make_canvas("100:1", [_make_frame("11:1", "A")])

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_versions = AsyncMock(return_value=[
        _vs(id="v3", created_at="2026-04-03T12:00:00Z", handle="bart"),
        _vs(id="v2", created_at="2026-04-02T12:00:00Z", label="checkpoint", handle="jakub"),
        _vs(id="v1", created_at="2026-03-25T12:00:00Z", handle="bart"),
    ])
    mock_client.get_file_meta = AsyncMock(return_value=_recent_meta())
    mock_client.get_file_shallow = AsyncMock(
        side_effect=_mock_get_file_shallow([old_canvas], [canvas]),
    )

    with patch.object(diff_module, "FigmaClient") as MockCls:
        MockCls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockCls.return_value.__aexit__ = AsyncMock(return_value=False)

        results, _, _ = await _run("fake-key", tmp_path / "figma", "7d")

    fd = results[0]
    assert len(fd.versions_in_range) == 2
    users = {v.user for v in fd.versions_in_range}
    assert "bart" in users
    assert "jakub" in users


# ── CLI tests ──────────────────────────────────────────────────────


def test_cli_missing_api_key(tmp_path: Path) -> None:
    """Command fails with clear error when FIGMA_API_KEY is not set."""
    from click.testing import CliRunner
    from figmaclaw.main import cli

    figma_dir = tmp_path / "figma"
    figma_dir.mkdir()

    runner = CliRunner(env={"FIGMA_API_KEY": ""})
    result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "diff", "figma/"])
    assert result.exit_code != 0
    assert "FIGMA_API_KEY" in result.output


def test_cli_json_output(tmp_path: Path) -> None:
    """--format json produces valid JSON."""
    from click.testing import CliRunner
    from figmaclaw.main import cli

    figma_dir = tmp_path / "figma" / "app"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-100-1.md").write_text(_PAGE_MD)

    canvas = _make_canvas("100:1", [_make_frame("11:1", "A"), _make_frame("11:3", "New")])
    old_canvas = _make_canvas("100:1", [_make_frame("11:1", "A")])

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_versions = AsyncMock(return_value=[
        _vs(id="v2", created_at="2026-04-03T12:00:00Z", handle="bart"),
        _vs(id="v1", created_at="2026-03-25T12:00:00Z"),
    ])
    mock_client.get_file_meta = AsyncMock(return_value=_recent_meta())
    mock_client.get_file_shallow = AsyncMock(
        side_effect=_mock_get_file_shallow([old_canvas], [canvas]),
    )

    with patch.object(diff_module, "FigmaClient") as MockCls:
        MockCls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockCls.return_value.__aexit__ = AsyncMock(return_value=False)

        runner = CliRunner(env={"FIGMA_API_KEY": "fake"})
        result = runner.invoke(cli, [
            "--repo-dir", str(tmp_path), "diff", "figma/",
            "--format", "json", "--no-progress",
        ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "files" in data
    assert len(data["files"]) == 1
    assert len(data["files"][0]["pages"]) == 1
    assert data["files"][0]["pages"][0]["added_frames"][0]["node_id"] == "11:3"


# ── VersionSummary nullable fields ────────────────────────────────


def test_version_summary_accepts_null_label_and_description() -> None:
    """Figma API returns null for label/description on autosave versions."""
    v = VersionSummary(
        id="v1",
        created_at="2026-04-01T12:00:00Z",
        label=None,
        description=None,
        user=VersionUser(handle="bart"),
    )
    assert v.label is None
    assert v.description is None


def test_version_summary_defaults_to_empty_string() -> None:
    """When label/description are omitted, they default to empty string."""
    v = VersionSummary(
        id="v1",
        created_at="2026-04-01T12:00:00Z",
        user=VersionUser(handle="bart"),
    )
    assert v.label == ""
    assert v.description == ""


@pytest.mark.asyncio
async def test_find_version_before_with_null_labels() -> None:
    """Versions with null labels should not crash version parsing."""
    from datetime import datetime, timezone

    client = MagicMock(spec=FigmaClient)
    client.get_versions = AsyncMock(return_value=[
        VersionSummary(
            id="v2", created_at="2026-04-03T12:00:00Z",
            label=None, description=None,
            user=VersionUser(handle="bart"),
        ),
        VersionSummary(
            id="v1", created_at="2026-03-25T12:00:00Z",
            label=None, description=None,
            user=VersionUser(handle="jakub"),
        ),
    ])

    cutoff = datetime(2026, 3, 28, tzinfo=timezone.utc)
    old, in_range = await _find_version_before(client, "abc123", cutoff)

    assert old is not None
    assert old.id == "v1"
    assert len(in_range) == 1
    assert in_range[0].id == "v2"
