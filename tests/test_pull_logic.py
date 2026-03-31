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

from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection  # noqa: F401 — used in tests
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


def _fake_component_page_node(page_id: str = "7741:45837") -> dict:
    """A CANVAS page whose only section is a COMPONENT_SET-based component library."""
    return {
        "id": page_id,
        "name": "Components",
        "type": "CANVAS",
        "children": [
            {
                "id": "20:1",
                "name": "buttons",
                "type": "SECTION",
                "children": [
                    {"id": "30:1", "name": "Button / Primary", "type": "COMPONENT_SET", "children": []},
                    {"id": "30:2", "name": "Button / Secondary", "type": "COMPONENT_SET", "children": []},
                ],
            }
        ],
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
    out = tmp_path / "figma" / "web-app" / "pages" / "onboarding-7741-45837.md"
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
async def test_pull_file_writes_component_md_for_component_section(tmp_path: Path):
    """INVARIANT: pull_file writes a components/*.md for each component library section."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=_fake_component_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.component_sections_written == 1
    assert result.pages_written == 0  # no screen sections
    out = tmp_path / "figma" / "web-app" / "components" / "buttons-20-1.md"
    assert out.exists()
    assert "## Variants" in out.read_text()


@pytest.mark.asyncio
async def test_pull_file_skips_screen_md_when_all_sections_are_components(tmp_path: Path):
    """INVARIANT: No pages/*.md is written when a page has only component library sections."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=_fake_component_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 0
    pages_dir = tmp_path / "figma" / "web-app" / "pages"
    assert not pages_dir.exists() or not any(pages_dir.iterdir())


@pytest.mark.asyncio
async def test_pull_file_manifest_records_component_paths(tmp_path: Path):
    """INVARIANT: Manifest entry stores component_md_paths after writing component sections."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=_fake_component_page_node())

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    entry = state.manifest.files["abc123"].pages["7741:45837"]
    assert entry.md_path is None  # no screen sections
    assert "figma/web-app/components/buttons-20-1.md" in entry.component_md_paths


@pytest.mark.asyncio
async def test_pull_file_preserves_component_descriptions_from_existing_md(tmp_path: Path):
    """INVARIANT: pull_file reads existing component .md descriptions before overwriting."""
    from figmaclaw.figma_models import FigmaSection, FigmaFrame
    from figmaclaw.figma_render import render_component_section

    # Pre-write a component .md with existing descriptions
    comp_section = FigmaSection(
        node_id="20:1",
        name="buttons",
        frames=[
            FigmaFrame(node_id="30:1", name="Button / Primary", description="Primary CTA."),
        ],
        is_component_library=True,
    )
    page_stub = FigmaPage(
        file_key="abc123", file_name="Web App", page_node_id="7741:45837",
        page_name="Components", page_slug="components-7741-45837",
        figma_url="", sections=[comp_section], flows=[], version="v1", last_modified="",
    )
    comp_out = tmp_path / "figma" / "web-app" / "components" / "buttons-20-1.md"
    comp_out.parent.mkdir(parents=True, exist_ok=True)
    comp_out.write_text(render_component_section(comp_section, page_stub, "0000000000000000"))

    # Now run pull — the existing description should be preserved
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    from figmaclaw.figma_client import FigmaClient
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=_fake_component_page_node())

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    content = comp_out.read_text()
    assert "Primary CTA." in content


@pytest.mark.asyncio
async def test_pull_file_preserves_existing_descriptions(tmp_path: Path):
    """INVARIANT: pull_file preserves frame descriptions from existing .md for unchanged frames."""
    # Pre-write a .md with existing descriptions at the slug-based path
    existing_entry = _make_entry("0000000000000000")
    existing_entry = existing_entry.model_copy(update={"md_path": "figma/web-app/pages/onboarding-7741-45837.md"})
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

    out = tmp_path / "figma" / "web-app" / "pages" / "onboarding-7741-45837.md"
    content = out.read_text()
    # The existing descriptions should be preserved in the output
    assert "Welcome screen." in content
    assert "Camera access prompt." in content


# --- max_pages / has_more ---

def _fake_file_meta_multi(n_pages: int) -> dict:
    """File meta with n_pages CANVAS children."""
    return {
        "version": "v2",
        "lastModified": "2026-03-31T12:00:00Z",
        "name": "Web App",
        "document": {
            "children": [
                {"id": f"100:{i}", "name": f"Page {i}", "type": "CANVAS"}
                for i in range(1, n_pages + 1)
            ]
        },
    }


def _fake_page_node_for_id(page_id: str, page_name: str) -> dict:
    return {
        "id": page_id,
        "name": page_name,
        "type": "CANVAS",
        "children": [
            {
                "id": f"s:{page_id}",
                "name": "section",
                "type": "SECTION",
                "children": [
                    {"id": f"f:{page_id}", "name": "frame", "type": "FRAME", "children": []},
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_pull_file_respects_max_pages(tmp_path: Path):
    """INVARIANT: pull_file writes at most max_pages pages when max_pages is set."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    n_pages = 6
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(n_pages))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False, max_pages=3)

    assert result.pages_written == 3
    assert result.has_more is True


@pytest.mark.asyncio
async def test_pull_file_has_more_false_when_all_pages_written(tmp_path: Path):
    """INVARIANT: has_more is False when all pages fit within max_pages."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(2))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False, max_pages=5)

    assert result.pages_written == 2
    assert result.has_more is False


@pytest.mark.asyncio
async def test_pull_file_has_more_false_when_no_limit(tmp_path: Path):
    """INVARIANT: has_more is False when max_pages is not set."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(4))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.has_more is False


# --- pages_errored ---

@pytest.mark.asyncio
async def test_pull_file_increments_pages_errored_on_fetch_failure(tmp_path: Path):
    """INVARIANT: pull_file increments pages_errored when a page fetch fails."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(2))
    # First page raises, second succeeds
    mock_client.get_page = AsyncMock(
        side_effect=[
            Exception("network error"),
            _fake_page_node_for_id("100:2", "Page 2"),
        ]
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_errored == 1
    assert result.pages_written == 1  # second page still written


@pytest.mark.asyncio
async def test_pull_file_continues_after_page_fetch_error(tmp_path: Path):
    """INVARIANT: a single page fetch error does not abort processing of remaining pages."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    n_pages = 3
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(n_pages))
    mock_client.get_page = AsyncMock(
        side_effect=[
            Exception("quota exceeded"),
            _fake_page_node_for_id("100:2", "Page 2"),
            _fake_page_node_for_id("100:3", "Page 3"),
        ]
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_errored == 1
    assert result.pages_written == 2


# --- skip_pages ---

def _fake_file_meta_with_pages(*page_names: str) -> dict:
    return {
        "version": "v2",
        "lastModified": "2026-03-31T12:00:00Z",
        "name": "Web App",
        "document": {
            "children": [
                {"id": f"100:{i}", "name": name, "type": "CANVAS"}
                for i, name in enumerate(page_names, 1)
            ]
        },
    }


@pytest.mark.asyncio
async def test_pull_file_skips_pages_matching_skip_pages_patterns(tmp_path: Path):
    """INVARIANT: pull_file skips pages whose names match skip_pages glob patterns."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_with_pages(
        "Onboarding", "old-components", "old concept", "---"
    ))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, "Onboarding")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    # Only "Onboarding" should be written; the 3 skip-pattern pages are skipped
    assert result.pages_written == 1
    assert result.pages_skipped == 3
    # get_page should never be called for skipped pages (saves API calls)
    assert mock_client.get_page.call_count == 1


@pytest.mark.asyncio
async def test_pull_file_skip_pages_does_not_fetch_page_content(tmp_path: Path):
    """INVARIANT: Skipped pages are filtered before any API fetch — no wasted calls."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_with_pages("old-archive"))
    mock_client.get_page = AsyncMock()

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_skipped == 1
    assert result.pages_written == 0
    mock_client.get_page.assert_not_called()


# --- progress callback ---

@pytest.mark.asyncio
async def test_pull_file_calls_progress_for_each_page(tmp_path: Path):
    """INVARIANT: pull_file calls the progress callback for each page processed."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(2))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, f"Page {pid}")
    )

    progress_messages: list[str] = []
    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False, progress=progress_messages.append)

    assert result.pages_written == 2
    # Should have received progress messages for the two pages
    assert len(progress_messages) >= 2
    assert any("[1/2]" in m for m in progress_messages)
    assert any("[2/2]" in m for m in progress_messages)


# --- on_page_written callback ---

@pytest.mark.asyncio
async def test_pull_file_calls_on_page_written_for_each_written_page(tmp_path: Path):
    """INVARIANT: pull_file calls on_page_written after each page is written to disk."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(2))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, f"Page {pid}")
    )

    written_labels: list[str] = []
    result = await pull_file(
        mock_client, "abc123", state, tmp_path, force=False,
        on_page_written=lambda label, paths: written_labels.append(label),
    )

    assert result.pages_written == 2
    assert len(written_labels) == 2
    assert all("Web App" in label for label in written_labels)


@pytest.mark.asyncio
async def test_pull_file_on_page_written_receives_written_paths(tmp_path: Path):
    """INVARIANT: on_page_written receives the list of paths that were written."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(1))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, f"Page {pid}")
    )

    received_paths: list[list[str]] = []
    await pull_file(
        mock_client, "abc123", state, tmp_path, force=False,
        on_page_written=lambda label, paths: received_paths.append(paths),
    )

    assert len(received_paths) == 1
    assert len(received_paths[0]) >= 1
    assert all(isinstance(p, str) for p in received_paths[0])


@pytest.mark.asyncio
async def test_pull_file_skipped_pages_do_not_trigger_on_page_written(tmp_path: Path):
    """INVARIANT: on_page_written is NOT called for pages that are skipped (hash unchanged)."""
    from figmaclaw.figma_hash import compute_page_hash
    from figmaclaw.figma_client import FigmaClient

    page_node = _fake_page_node()
    stored_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Onboarding", page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash=stored_hash, last_refreshed_at="2026-03-30T00:00:00Z",
    )

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)

    written_labels: list[str] = []
    result = await pull_file(
        mock_client, "abc123", state, tmp_path, force=False,
        on_page_written=lambda label, paths: written_labels.append(label),
    )

    assert result.pages_skipped == 1
    assert len(written_labels) == 0


# --- parallel fetch (max_pages=None fetches pages concurrently) ---

@pytest.mark.asyncio
async def test_pull_file_parallel_fetch_writes_all_pages(tmp_path: Path):
    """INVARIANT: without max_pages, all pages are fetched and written (parallel path)."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    n_pages = 4
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(n_pages))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: _fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == n_pages
    assert result.has_more is False


@pytest.mark.asyncio
async def test_pull_file_parallel_fetch_handles_individual_page_errors(tmp_path: Path):
    """INVARIANT: parallel fetch tolerates individual page errors and processes the rest."""
    from figmaclaw.figma_client import FigmaClient

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=_fake_file_meta_multi(3))
    mock_client.get_page = AsyncMock(
        side_effect=[
            _fake_page_node_for_id("100:1", "Page 1"),
            Exception("timeout"),
            _fake_page_node_for_id("100:3", "Page 3"),
        ]
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 2
    assert result.pages_errored == 1


# Tests for --team-id listing pre-filter in commands/pull.py

from figmaclaw.commands.pull import _listing_prefilter
from figmaclaw.figma_client import FigmaClient


def _make_state_with_file(tmp_path: Path, file_key: str, last_modified: str) -> FigmaSyncState:
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(file_key, "My File")
    state.manifest.files[file_key].last_modified = last_modified
    return state


@pytest.mark.asyncio
async def test_listing_prefilter_returns_last_modified_for_each_file(tmp_path: Path):
    """INVARIANT: _listing_prefilter returns {file_key: last_modified} for all files in listing."""
    state = FigmaSyncState(tmp_path)
    state.load()
    client = MagicMock(spec=FigmaClient)
    client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    client.list_project_files = AsyncMock(return_value=[
        {"key": "fileA", "name": "App", "last_modified": "2026-03-01T00:00:00Z"},
        {"key": "fileB", "name": "DS",  "last_modified": "2026-02-01T00:00:00Z"},
    ])

    result = await _listing_prefilter(client, "team123", state, "all")

    assert result == {
        "fileA": "2026-03-01T00:00:00Z",
        "fileB": "2026-02-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_listing_prefilter_tracks_new_files(tmp_path: Path):
    """INVARIANT: _listing_prefilter adds newly discovered files to the manifest."""
    state = FigmaSyncState(tmp_path)
    state.load()
    client = MagicMock(spec=FigmaClient)
    client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    client.list_project_files = AsyncMock(return_value=[
        {"key": "fileA", "name": "New File", "last_modified": "2026-03-01T00:00:00Z"},
    ])

    await _listing_prefilter(client, "team123", state, "all")

    assert "fileA" in state.manifest.tracked_files


@pytest.mark.asyncio
async def test_listing_prefilter_does_not_duplicate_existing_tracked_files(tmp_path: Path):
    """INVARIANT: _listing_prefilter is idempotent for already-tracked files."""
    state = _make_state_with_file(tmp_path, "fileA", "2026-03-01T00:00:00Z")
    client = MagicMock(spec=FigmaClient)
    client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    client.list_project_files = AsyncMock(return_value=[
        {"key": "fileA", "name": "App", "last_modified": "2026-03-01T00:00:00Z"},
    ])

    await _listing_prefilter(client, "team123", state, "all")

    assert state.manifest.tracked_files.count("fileA") == 1


@pytest.mark.asyncio
async def test_listing_prefilter_applies_since_filter_to_new_files(tmp_path: Path):
    """INVARIANT: --since filter applies to new file discovery, not already-tracked files."""
    state = FigmaSyncState(tmp_path)
    state.load()
    client = MagicMock(spec=FigmaClient)
    client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    client.list_project_files = AsyncMock(return_value=[
        {"key": "old_file", "name": "Old",  "last_modified": "2020-01-01T00:00:00Z"},
        {"key": "new_file", "name": "New",  "last_modified": "2026-03-01T00:00:00Z"},
    ])

    result = await _listing_prefilter(client, "team123", state, "3m")

    assert "old_file" not in state.manifest.tracked_files
    assert "new_file" in state.manifest.tracked_files
    # old_file still in the returned dict (its last_modified may be useful for pull filtering)
    assert "new_file" in result


@pytest.mark.asyncio
async def test_pull_cmd_skips_unchanged_files_via_listing(tmp_path: Path):
    """INVARIANT: when --team-id is set, files whose listing last_modified matches stored
    value are skipped without any get_file_meta call."""
    from figmaclaw.commands.pull import _run

    state = _make_state_with_file(tmp_path, "fileA", "2026-03-01T00:00:00Z")
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    mock_client.list_project_files = AsyncMock(return_value=[
        {"key": "fileA", "name": "App", "last_modified": "2026-03-01T00:00:00Z"},  # unchanged
    ])
    mock_client.get_file_meta = AsyncMock()

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        await _run("key", tmp_path, None, False, True, None, False, 10, "team123", "all")

    mock_client.get_file_meta.assert_not_called()


@pytest.mark.asyncio
async def test_pull_cmd_pulls_files_whose_listing_last_modified_changed(tmp_path: Path):
    """INVARIANT: files with a changed listing last_modified proceed to get_file_meta."""
    from figmaclaw.commands.pull import _run

    state = _make_state_with_file(tmp_path, "fileA", "2026-01-01T00:00:00Z")
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    mock_client.list_project_files = AsyncMock(return_value=[
        {"key": "fileA", "name": "App", "last_modified": "2026-03-01T00:00:00Z"},  # changed
    ])
    mock_client.get_file_meta = AsyncMock(return_value={
        "version": "v2", "lastModified": "2026-03-01T00:00:00Z",
        "name": "App", "document": {"children": []},
    })

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        await _run("key", tmp_path, None, False, True, None, False, 10, "team123", "all")

    mock_client.get_file_meta.assert_called_once_with("fileA")


@pytest.mark.asyncio
async def test_pull_cmd_skips_figjam_files_not_in_listing(tmp_path: Path):
    """INVARIANT: files absent from the team listing (e.g. FigJam boards) are always
    skipped — they cannot change if not reachable via the listing API."""
    from figmaclaw.commands.pull import _run

    state = _make_state_with_file(tmp_path, "figjam_key", "")
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    mock_client.list_project_files = AsyncMock(return_value=[])  # FigJam not in listing
    mock_client.get_file_meta = AsyncMock()

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        await _run("key", tmp_path, None, False, True, None, False, 10, "team123", "all")

    mock_client.get_file_meta.assert_not_called()


@pytest.mark.asyncio
async def test_pull_cmd_stamps_listing_last_modified_after_failed_get_file_meta(tmp_path: Path):
    """INVARIANT: when get_file_meta fails (skipped_file=True) and the listing provides
    a last_modified, that value is stored in the manifest so the next run pre-filters
    the file without making another wasted API call."""
    from figmaclaw.commands.pull import _run
    from figmaclaw.pull_logic import PullResult

    # File has empty last_modified (never successfully pulled)
    state = _make_state_with_file(tmp_path, "restricted_key", "")
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_team_projects = AsyncMock(return_value=[{"id": "p1", "name": "Web"}])
    mock_client.list_project_files = AsyncMock(return_value=[
        {"key": "restricted_key", "name": "Restricted", "last_modified": "2026-03-01T00:00:00Z"},
    ])
    # get_file_meta fails (400 / permission error) → pull_file returns skipped_file=True
    failed_result = PullResult(file_key="restricted_key", skipped_file=True)
    mock_pull = AsyncMock(return_value=failed_result)

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        with patch("figmaclaw.commands.pull.pull_file", mock_pull):
            await _run("key", tmp_path, None, False, True, None, False, 10, "team123", "all")

    # Manifest should now have the listing's last_modified stamped in
    reloaded = FigmaSyncState(tmp_path)
    reloaded.load()
    assert reloaded.manifest.files["restricted_key"].last_modified == "2026-03-01T00:00:00Z"
