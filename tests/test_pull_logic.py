"""Tests for the incremental pull logic.

INVARIANTS:
- pull_file skips pages whose hash hasn't changed (no filesystem write)
- pull_file writes .md files for pages with changed hashes
- pull_file updates the manifest after writing
- pull_file skips file entirely when version and lastModified unchanged (not --force)
- write_page creates parent dirs and writes rendered markdown
- existing frame descriptions are preserved for unchanged frames (LLM idempotency)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry
from figmaclaw.pull_logic import PullResult, pull_file, write_page


def _make_page(page_node_id: str = "7741:45837", page_name: str = "Onboarding") -> FigmaPage:
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description="Welcome screen."),
        FigmaFrame(node_id="11:2", name="permissions", description="Camera access prompt."),
    ]
    section = FigmaSection(node_id="10:1", name="intro", frames=frames)
    return FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id=page_node_id,
        page_name=page_name,
        page_slug="onboarding",
        figma_url="https://www.figma.com/design/abc123?node-id=7741-45837",
        sections=[section],
        flows=[],
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
    )


def _make_entry(page_hash: str = "aaaa1111bbbb2222") -> PageEntry:
    return PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T00:00:00Z",
    )


# --- write_page ---

def test_write_page_creates_file(tmp_path: Path):
    """INVARIANT: write_page creates the .md file at the correct path."""
    page = _make_page()
    entry = _make_entry()
    write_page(tmp_path, page, entry)
    out = tmp_path / "figma" / "abc123" / "pages" / "onboarding.md"
    assert out.exists()
    assert "# Web App / Onboarding" in out.read_text()


def test_write_page_creates_parent_dirs(tmp_path: Path):
    """INVARIANT: write_page creates all intermediate directories."""
    page = _make_page()
    entry = _make_entry()
    write_page(tmp_path, page, entry)
    assert (tmp_path / "figma" / "abc123" / "pages").is_dir()


def test_write_page_returns_path(tmp_path: Path):
    """INVARIANT: write_page returns the Path where the file was written."""
    page = _make_page()
    entry = _make_entry()
    result = write_page(tmp_path, page, entry)
    assert result == tmp_path / "figma" / "abc123" / "pages" / "onboarding.md"


# --- pull_file ---

def _fake_file_meta(version: str = "v2", last_modified: str = "2026-03-31T12:00:00Z") -> dict:
    return {
        "version": version,
        "lastModified": last_modified,
        "name": "Web App",
        "document": {
            "children": [
                {"id": "7741:45837", "name": "Onboarding", "type": "CANVAS"}
            ]
        },
    }


def _fake_page_node(page_id: str = "7741:45837") -> dict:
    return {
        "id": page_id,
        "name": "Onboarding",
        "type": "CANVAS",
        "children": [
            {
                "id": "10:1",
                "name": "intro",
                "type": "SECTION",
                "children": [
                    {"id": "11:1", "name": "welcome", "type": "FRAME", "children": []},
                    {"id": "11:2", "name": "permissions", "type": "FRAME", "children": []},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_pull_file_skips_when_version_unchanged(tmp_path: Path):
    """INVARIANT: pull_file returns skipped=True when file version is unchanged."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2", "2026-03-31T12:00:00Z"))

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.skipped_file is True
    assert result.pages_written == 0
    mock_client.get_file_meta.assert_called_once_with("abc123")


@pytest.mark.asyncio
async def test_pull_file_force_bypasses_version_check(tmp_path: Path):
    """INVARIANT: pull_file with force=True proceeds even when version matches."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=_fake_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=True)

    assert result.skipped_file is False


@pytest.mark.asyncio
async def test_pull_file_skips_page_when_hash_unchanged(tmp_path: Path):
    """INVARIANT: pull_file skips individual pages whose structural hash is unchanged."""
    from figmaclaw.figma_hash import compute_page_hash
    page_node = _fake_page_node()
    stored_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"  # old version → triggers page check
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash=stored_hash,
        last_refreshed_at="2026-03-30T00:00:00Z",
    )

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 0
    assert result.pages_skipped == 1


@pytest.mark.asyncio
async def test_pull_file_writes_page_when_hash_changed(tmp_path: Path):
    """INVARIANT: pull_file writes an .md file when a page's hash has changed."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    # Old version so it doesn't skip
    state.manifest.files["abc123"].version = "v1"
    # Hash in manifest doesn't match what we'll compute
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash="0000000000000000",
        last_refreshed_at="2026-03-30T00:00:00Z",
    )

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=_fake_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 1
    out = tmp_path / "figma" / "web-app" / "pages" / "onboarding.md"
    assert out.exists()


@pytest.mark.asyncio
async def test_pull_file_updates_manifest_after_write(tmp_path: Path):
    """INVARIANT: pull_file updates the manifest with the new hash after writing."""
    from figmaclaw.figma_hash import compute_page_hash
    page_node = _fake_page_node()
    new_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert state.manifest.files["abc123"].pages["7741:45837"].page_hash == new_hash
    assert state.manifest.files["abc123"].version == "v2"


@pytest.mark.asyncio
async def test_pull_file_preserves_existing_descriptions(tmp_path: Path):
    """INVARIANT: pull_file preserves frame descriptions from existing .md for unchanged frames."""
    # Pre-write a .md with existing descriptions at the slug-based path
    existing_entry = _make_entry("0000000000000000")
    existing_entry = existing_entry.model_copy(update={"md_path": "figma/web-app/pages/onboarding.md"})
    page_with_descs = _make_page()  # has descriptions
    write_page(tmp_path, page_with_descs, existing_entry)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash="0000000000000000",
        last_refreshed_at="2026-03-30T00:00:00Z",
    )

    page_node = _fake_page_node()

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    out = tmp_path / "figma" / "web-app" / "pages" / "onboarding.md"
    content = out.read_text()
    # The existing descriptions should be preserved in the output
    assert "Welcome screen." in content
    assert "Camera access prompt." in content
