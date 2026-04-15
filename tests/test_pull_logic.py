"""Tests for the incremental pull logic.

INVARIANTS:
- pull_file skips pages whose hash hasn't changed (no filesystem write)
- pull_file writes .md files for pages with changed hashes
- pull_file updates the manifest after writing
- pull_file skips file entirely when version and lastModified unchanged (not --force)
- write_new_page creates parent dirs and writes rendered markdown
- existing frame descriptions are preserved for unchanged frames (LLM idempotency)
- pull_file is idempotent: second call on unchanged Figma content must not modify any file
- has_more=True is only set when content-changed pages exhaust the budget, never by schema upgrades
- schema upgrades always converge in a single pass regardless of max_pages

Schema version registry
-----------------------
Every time CURRENT_PULL_SCHEMA_VERSION is bumped, a convergence test must be added
for the upgrade path FROM the previous version. Add the old version to
TESTED_UPGRADE_FROM_VERSIONS below and add a corresponding test.

Current: see test_schema_upgrade_does_not_cause_infinite_loop_with_max_pages (v2→v3).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.commands.pull import _listing_prefilter
from figmaclaw.figma_api_models import FileMetaResponse, FileSummary, ProjectSummary
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection  # noqa: F401 — used in tests
from figmaclaw.figma_paths import page_path, slugify
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry
from figmaclaw.pull_logic import PullResult, pull_file, write_new_page
from tests.conftest import (
    PullEnv,
    fake_component_page_node,
    fake_file_meta,
    fake_file_meta_multi,
    fake_file_meta_with_pages,
    fake_get_nodes_response,
    fake_page_node,
    fake_page_node_for_id,
    fake_page_node_with_children,
)

# Schema version registry — must contain every version that has been superseded.
# When you bump CURRENT_PULL_SCHEMA_VERSION from N to N+1, add N here and add
# a corresponding convergence test (like test_schema_upgrade_does_not_cause_infinite_loop_with_max_pages).
TESTED_UPGRADE_FROM_VERSIONS: frozenset[int] = frozenset({1, 2, 3, 4, 5})


def test_schema_upgrade_coverage_is_current():
    """INVARIANT: every superseded pull schema version has a convergence upgrade test.

    If this test fails after bumping CURRENT_PULL_SCHEMA_VERSION, add the old version
    to TESTED_UPGRADE_FROM_VERSIONS above and write a new convergence test.
    """
    expected = frozenset(range(1, CURRENT_PULL_SCHEMA_VERSION))
    assert expected == TESTED_UPGRADE_FROM_VERSIONS, (
        f"CURRENT_PULL_SCHEMA_VERSION={CURRENT_PULL_SCHEMA_VERSION} but "
        f"TESTED_UPGRADE_FROM_VERSIONS only covers {sorted(TESTED_UPGRADE_FROM_VERSIONS)}. "
        "Add N to TESTED_UPGRADE_FROM_VERSIONS and write a convergence test for the N→N+1 upgrade."
    )


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
        md_path="figma/web-app-abc123/pages/onboarding.md",
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T00:00:00Z",
    )


# --- write_new_page ---


def test_write_new_page_creates_file(tmp_path: Path):
    """INVARIANT: write_new_page creates the .md file at the correct path."""
    page = _make_page()
    entry = _make_entry()
    write_new_page(tmp_path, page, entry)
    out = tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding.md"
    assert out.exists()
    assert "# Web App / Onboarding" in out.read_text()


def test_write_new_page_creates_parent_dirs(tmp_path: Path):
    """INVARIANT: write_new_page creates all intermediate directories."""
    page = _make_page()
    entry = _make_entry()
    write_new_page(tmp_path, page, entry)
    assert (tmp_path / "figma" / "web-app-abc123" / "pages").is_dir()


def test_write_new_page_returns_path(tmp_path: Path):
    """INVARIANT: write_new_page returns the Path where the file was written."""
    page = _make_page()
    entry = _make_entry()
    result = write_new_page(tmp_path, page, entry)
    assert result == tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding.md"


# --- pull_file ---


@pytest.mark.asyncio
async def test_pull_file_skips_when_version_unchanged(tmp_path: Path):
    """INVARIANT: pull_file returns skipped=True when file version is unchanged and schema is current."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files["abc123"].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))

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

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=fake_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=True)

    assert result.skipped_file is False


@pytest.mark.asyncio
async def test_pull_file_skips_page_when_hash_unchanged(tmp_path: Path):
    """INVARIANT: pull_file skips individual pages whose structural hash is unchanged (when schema is current)."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
    from figmaclaw.figma_hash import compute_page_hash

    page_node = fake_page_node()
    stored_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"  # old version → triggers page check
    state.manifest.files[
        "abc123"
    ].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION  # schema current → page skip applies
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/web-app-abc123/pages/onboarding-7741-45837.md",
        page_hash=stored_hash,
        last_refreshed_at="2026-03-30T00:00:00Z",
    )
    existing_md = tmp_path / "figma/web-app-abc123/pages/onboarding-7741-45837.md"
    existing_md.parent.mkdir(parents=True, exist_ok=True)
    existing_md.write_text("---\n---\n")
    existing_md.with_suffix(".tokens.json").write_text('{"schema_version":2}')

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
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
        md_path="figma/web-app-abc123/pages/onboarding.md",
        page_hash="0000000000000000",
        last_refreshed_at="2026-03-30T00:00:00Z",
    )

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=fake_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 1
    out = tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding-7741-45837.md"
    assert out.exists()


@pytest.mark.asyncio
async def test_pull_file_updates_manifest_after_write(tmp_path: Path):
    """INVARIANT: pull_file updates the manifest with the new hash after writing."""
    from figmaclaw.figma_hash import compute_page_hash

    page_node = fake_page_node()
    new_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert state.manifest.files["abc123"].pages["7741:45837"].page_hash == new_hash
    assert state.manifest.files["abc123"].version == "v2"


@pytest.mark.asyncio
async def test_pull_file_writes_component_md_for_component_section(pull_env: PullEnv):
    """INVARIANT: pull_file writes a components/*.md for each component library section."""
    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.component_sections_written == 1
    assert result.pages_written == 0  # no screen sections
    out = tmp_path / "figma" / "web-app-abc123" / "components" / "buttons-20-1.md"
    assert out.exists()
    assert "## Variants" in out.read_text()


@pytest.mark.asyncio
async def test_pull_file_skips_screen_md_when_all_sections_are_components(pull_env: PullEnv):
    """INVARIANT: No pages/*.md is written when a page has only component library sections."""
    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 0
    pages_dir = tmp_path / "figma" / "web-app-abc123" / "pages"
    assert not pages_dir.exists() or not any(pages_dir.iterdir())


@pytest.mark.asyncio
async def test_pull_file_manifest_records_component_paths(pull_env: PullEnv):
    """INVARIANT: Manifest entry stores component_md_paths after writing component sections."""
    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    entry = state.manifest.files["abc123"].pages["7741:45837"]
    assert entry.md_path is None  # no screen sections
    assert "figma/web-app-abc123/components/buttons-20-1.md" in entry.component_md_paths


@pytest.mark.asyncio
async def test_pull_file_writes_component_section_with_frame_ids(pull_env: PullEnv):
    """INVARIANT: pull_file writes component .md with frame IDs in frontmatter."""
    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    comp_out = tmp_path / "figma" / "web-app-abc123" / "components" / "buttons-20-1.md"
    content = comp_out.read_text()
    from figmaclaw.figma_parse import parse_frontmatter

    fm = parse_frontmatter(content)
    assert fm is not None
    assert isinstance(fm.frames, list)
    assert "30:1" in fm.frames


@pytest.mark.asyncio
async def test_pull_file_preserves_existing_component_descriptions_on_changed_page(tmp_path: Path):
    """INVARIANT: pull_file must not rewrite existing component body prose on updates."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Components",
        page_slug="components-7741-45837",
        md_path=None,
        page_hash="0000000000000000",
        last_refreshed_at="2026-03-30T00:00:00Z",
        component_md_paths=["figma/web-app-abc123/components/buttons-20-1.md"],
    )

    comp_path = tmp_path / "figma/web-app-abc123/components/buttons-20-1.md"
    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comp_path.write_text(
        """---
file_key: abc123
page_node_id: '7741:45837'
section_node_id: '20:1'
frames: ['30:1', '30:2']
enriched_hash: keep-me
---

# Web App / Components / buttons

[Open in Figma](https://www.figma.com/design/abc123?node-id=20-1)

## Variants (`20:1`)

| Variant | Node ID | Description |
|---------|---------|-------------|
| Button / Primary | `30:1` | Keep this existing description |
| Button / Secondary | `30:2` | Keep this too |
"""
    )

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())
    mock_client.get_component_sets = AsyncMock(return_value=[])

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    updated = comp_path.read_text()
    assert "Keep this existing description" in updated
    assert "Keep this too" in updated
    assert "(no description yet)" not in updated


@pytest.mark.asyncio
async def test_pull_file_migrates_legacy_component_path_without_losing_descriptions(tmp_path: Path):
    """INVARIANT: legacy component path migration keeps existing human-written descriptions."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Components",
        page_slug="components-7741-45837",
        md_path=None,
        page_hash="0000000000000000",
        last_refreshed_at="2026-03-30T00:00:00Z",
        component_md_paths=["figma/web-app/components/buttons-20-1.md"],  # legacy path
    )

    legacy_path = tmp_path / "figma/web-app/components/buttons-20-1.md"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(
        """---
file_key: abc123
page_node_id: '7741:45837'
section_node_id: '20:1'
frames: ['30:1', '30:2']
---

# Web App / Components / buttons

[Open in Figma](https://www.figma.com/design/abc123?node-id=20-1)

## Variants (`20:1`)

| Variant | Node ID | Description |
|---------|---------|-------------|
| Button / Primary | `30:1` | Legacy migrated description |
| Button / Secondary | `30:2` | Another legacy description |
"""
    )

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())
    mock_client.get_component_sets = AsyncMock(return_value=[])

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    new_path = tmp_path / "figma/web-app-abc123/components/buttons-20-1.md"
    assert new_path.exists()
    migrated = new_path.read_text()
    assert "Legacy migrated description" in migrated
    assert "Another legacy description" in migrated
    assert "(no description yet)" not in migrated


@pytest.mark.asyncio
async def test_pull_file_component_description_preservation_is_idempotent(tmp_path: Path):
    """INVARIANT: component description preservation remains stable across repeated pulls."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Components",
        page_slug="components-7741-45837",
        md_path=None,
        page_hash="0000000000000000",
        last_refreshed_at="2026-03-30T00:00:00Z",
        component_md_paths=["figma/web-app-abc123/components/buttons-20-1.md"],
    )

    comp_path = tmp_path / "figma/web-app-abc123/components/buttons-20-1.md"
    comp_path.parent.mkdir(parents=True, exist_ok=True)
    comp_path.write_text(
        """---
file_key: abc123
page_node_id: '7741:45837'
section_node_id: '20:1'
frames: ['30:1', '30:2']
---

# Web App / Components / buttons

[Open in Figma](https://www.figma.com/design/abc123?node-id=20-1)

## Variants (`20:1`)

| Variant | Node ID | Description |
|---------|---------|-------------|
| Button / Primary | `30:1` | Idempotent description A |
| Button / Secondary | `30:2` | Idempotent description B |
"""
    )

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())

    # First run: content-changed path should preserve body prose.
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    first = comp_path.read_text()
    assert "Idempotent description A" in first
    assert "Idempotent description B" in first

    # Second run: unchanged file-level metadata should skip with no body churn.
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    second = comp_path.read_text()
    assert second == first


@pytest.mark.asyncio
async def test_pull_file_preserves_existing_descriptions(tmp_path: Path):
    """INVARIANT: pull_file preserves frame descriptions from existing .md for unchanged frames."""
    # Pre-write a .md with existing descriptions at the slug-based path
    existing_entry = _make_entry("0000000000000000")
    existing_entry = existing_entry.model_copy(
        update={"md_path": "figma/web-app-abc123/pages/onboarding-7741-45837.md"}
    )
    page_with_descs = _make_page()  # has descriptions
    write_new_page(tmp_path, page_with_descs, existing_entry)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/web-app-abc123/pages/onboarding.md",
        page_hash="0000000000000000",
        last_refreshed_at="2026-03-30T00:00:00Z",
    )

    page_node = fake_page_node()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    out = tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding-7741-45837.md"
    content = out.read_text()
    # The existing descriptions should be preserved in the output
    assert "Welcome screen." in content
    assert "Camera access prompt." in content


# --- max_pages / has_more ---


@pytest.mark.asyncio
async def test_pull_file_respects_max_pages(tmp_path: Path):
    """INVARIANT: pull_file writes at most max_pages pages when max_pages is set."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    n_pages = 6
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(n_pages))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False, max_pages=3)

    assert result.pages_written == 3
    assert result.has_more is True


@pytest.mark.asyncio
async def test_pull_file_has_more_false_when_all_pages_written(tmp_path: Path):
    """INVARIANT: has_more is False when all pages fit within max_pages."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(2))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False, max_pages=5)

    assert result.pages_written == 2
    assert result.has_more is False


@pytest.mark.asyncio
async def test_pull_file_has_more_false_when_no_limit(tmp_path: Path):
    """INVARIANT: has_more is False when max_pages is not set."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(4))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.has_more is False


# --- pages_errored ---


@pytest.mark.asyncio
async def test_pull_file_increments_pages_errored_on_fetch_failure(tmp_path: Path):
    """INVARIANT: pull_file increments pages_errored when a page fetch fails."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(2))
    # First page raises, second succeeds
    mock_client.get_page = AsyncMock(
        side_effect=[
            Exception("network error"),
            fake_page_node_for_id("100:2", "Page 2"),
        ]
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_errored == 1
    assert result.pages_written == 1  # second page still written


@pytest.mark.asyncio
async def test_pull_file_continues_after_page_fetch_error(tmp_path: Path):
    """INVARIANT: a single page fetch error does not abort processing of remaining pages."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    n_pages = 3
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(n_pages))
    mock_client.get_page = AsyncMock(
        side_effect=[
            Exception("quota exceeded"),
            fake_page_node_for_id("100:2", "Page 2"),
            fake_page_node_for_id("100:3", "Page 3"),
        ]
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_errored == 1
    assert result.pages_written == 2


# --- skip_pages ---


@pytest.mark.asyncio
async def test_pull_file_skips_pages_matching_skip_pages_patterns(tmp_path: Path):
    """INVARIANT: pull_file skips pages whose names match skip_pages glob patterns."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(
        return_value=fake_file_meta_with_pages("Onboarding", "old-components", "old concept", "---")
    )
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, "Onboarding")
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

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_with_pages("old-archive"))
    mock_client.get_page = AsyncMock()

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_skipped == 1
    assert result.pages_written == 0
    mock_client.get_page.assert_not_called()


# --- progress callback ---


@pytest.mark.asyncio
async def test_pull_file_calls_progress_for_each_page(tmp_path: Path):
    """INVARIANT: pull_file calls the progress callback for each page processed."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(2))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, f"Page {pid}")
    )

    progress_messages: list[str] = []
    result = await pull_file(
        mock_client, "abc123", state, tmp_path, force=False, progress=progress_messages.append
    )

    assert result.pages_written == 2
    # Should have received progress messages for the two pages
    assert len(progress_messages) >= 2
    assert any("[1/2]" in m for m in progress_messages)
    assert any("[2/2]" in m for m in progress_messages)


# --- on_page_written callback ---


@pytest.mark.asyncio
async def test_pull_file_calls_on_page_written_for_each_written_page(tmp_path: Path):
    """INVARIANT: pull_file calls on_page_written after each page is written to disk."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(2))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, f"Page {pid}")
    )

    written_labels: list[str] = []
    result = await pull_file(
        mock_client,
        "abc123",
        state,
        tmp_path,
        force=False,
        on_page_written=lambda label, paths: written_labels.append(label),
    )

    assert result.pages_written == 2
    assert len(written_labels) == 2
    assert all("Web App" in label for label in written_labels)


@pytest.mark.asyncio
async def test_pull_file_on_page_written_receives_written_paths(tmp_path: Path):
    """INVARIANT: on_page_written receives the list of paths that were written."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(1))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, f"Page {pid}")
    )

    received_paths: list[list[str]] = []
    await pull_file(
        mock_client,
        "abc123",
        state,
        tmp_path,
        force=False,
        on_page_written=lambda label, paths: received_paths.append(paths),
    )

    assert len(received_paths) == 1
    assert len(received_paths[0]) >= 1
    assert all(isinstance(p, str) for p in received_paths[0])


@pytest.mark.asyncio
async def test_pull_file_skipped_pages_do_not_trigger_on_page_written(tmp_path: Path):
    """INVARIANT: on_page_written is NOT called for pages that are skipped (hash unchanged, schema current)."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
    from figmaclaw.figma_hash import compute_page_hash

    page_node = fake_page_node()
    stored_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION
    state.manifest.files["abc123"].pages["7741:45837"] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/web-app-abc123/pages/onboarding-7741-45837.md",
        page_hash=stored_hash,
        last_refreshed_at="2026-03-30T00:00:00Z",
    )
    existing_md = tmp_path / "figma/web-app-abc123/pages/onboarding-7741-45837.md"
    existing_md.parent.mkdir(parents=True, exist_ok=True)
    existing_md.write_text("---\n---\n")
    existing_md.with_suffix(".tokens.json").write_text('{"schema_version":2}')

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)

    written_labels: list[str] = []
    result = await pull_file(
        mock_client,
        "abc123",
        state,
        tmp_path,
        force=False,
        on_page_written=lambda label, paths: written_labels.append(label),
    )

    assert result.pages_skipped == 1
    assert len(written_labels) == 0


# --- parallel fetch (max_pages=None fetches pages concurrently) ---


@pytest.mark.asyncio
async def test_pull_file_parallel_fetch_writes_all_pages(tmp_path: Path):
    """INVARIANT: without max_pages, all pages are fetched and written (parallel path)."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    n_pages = 4
    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(n_pages))
    mock_client.get_page = AsyncMock(
        side_effect=lambda fk, pid: fake_page_node_for_id(pid, f"Page {pid}")
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == n_pages
    assert result.has_more is False


@pytest.mark.asyncio
async def test_pull_file_parallel_fetch_handles_individual_page_errors(tmp_path: Path):
    """INVARIANT: parallel fetch tolerates individual page errors and processes the rest."""

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta_multi(3))
    mock_client.get_page = AsyncMock(
        side_effect=[
            fake_page_node_for_id("100:1", "Page 1"),
            Exception("timeout"),
            fake_page_node_for_id("100:3", "Page 3"),
        ]
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 2
    assert result.pages_errored == 1


# Tests for --team-id listing pre-filter in commands/pull.py


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
    client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    client.list_project_files = AsyncMock(
        return_value=[
            FileSummary(key="fileA", name="App", last_modified="2026-03-01T00:00:00Z"),
            FileSummary(key="fileB", name="DS", last_modified="2026-02-01T00:00:00Z"),
        ]
    )

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
    client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    client.list_project_files = AsyncMock(
        return_value=[
            FileSummary(key="fileA", name="New File", last_modified="2026-03-01T00:00:00Z"),
        ]
    )

    await _listing_prefilter(client, "team123", state, "all")

    assert "fileA" in state.manifest.tracked_files


@pytest.mark.asyncio
async def test_listing_prefilter_does_not_duplicate_existing_tracked_files(tmp_path: Path):
    """INVARIANT: _listing_prefilter is idempotent for already-tracked files."""
    state = _make_state_with_file(tmp_path, "fileA", "2026-03-01T00:00:00Z")
    client = MagicMock(spec=FigmaClient)
    client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    client.list_project_files = AsyncMock(
        return_value=[
            FileSummary(key="fileA", name="App", last_modified="2026-03-01T00:00:00Z"),
        ]
    )

    await _listing_prefilter(client, "team123", state, "all")

    assert state.manifest.tracked_files.count("fileA") == 1


@pytest.mark.asyncio
async def test_listing_prefilter_applies_since_filter_to_new_files(tmp_path: Path):
    """INVARIANT: --since filter applies to new file discovery, not already-tracked files."""
    state = FigmaSyncState(tmp_path)
    state.load()
    client = MagicMock(spec=FigmaClient)
    client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    client.list_project_files = AsyncMock(
        return_value=[
            FileSummary(key="old_file", name="Old", last_modified="2020-01-01T00:00:00Z"),
            FileSummary(key="new_file", name="New", last_modified="2026-03-01T00:00:00Z"),
        ]
    )

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
    mock_client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    mock_client.list_project_files = AsyncMock(
        return_value=[
            FileSummary(key="fileA", name="App", last_modified="2026-03-01T00:00:00Z"),  # unchanged
        ]
    )
    mock_client.get_file_meta = AsyncMock()

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        await _run("key", tmp_path, None, False, None, False, 10, "team123", "all")

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
    mock_client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    mock_client.list_project_files = AsyncMock(
        return_value=[
            FileSummary(key="fileA", name="App", last_modified="2026-03-01T00:00:00Z"),  # changed
        ]
    )
    mock_client.get_file_meta = AsyncMock(
        return_value={
            "version": "v2",
            "lastModified": "2026-03-01T00:00:00Z",
            "name": "App",
            "document": {"children": []},
        }
    )

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        await _run("key", tmp_path, None, False, None, False, 10, "team123", "all")

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
    mock_client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    mock_client.list_project_files = AsyncMock(return_value=[])  # FigJam not in listing
    mock_client.get_file_meta = AsyncMock()

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        await _run("key", tmp_path, None, False, None, False, 10, "team123", "all")

    mock_client.get_file_meta.assert_not_called()


@pytest.mark.asyncio
async def test_pull_cmd_stamps_listing_last_modified_after_failed_get_file_meta(tmp_path: Path):
    """INVARIANT: when get_file_meta fails (skipped_file=True) and the listing provides
    a last_modified, that value is stored in the manifest so the next run pre-filters
    the file without making another wasted API call."""
    from figmaclaw.commands.pull import _run

    # File has empty last_modified (never successfully pulled)
    state = _make_state_with_file(tmp_path, "restricted_key", "")
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_team_projects = AsyncMock(return_value=[ProjectSummary(id="p1", name="Web")])
    mock_client.list_project_files = AsyncMock(
        return_value=[
            FileSummary(
                key="restricted_key", name="Restricted", last_modified="2026-03-01T00:00:00Z"
            ),
        ]
    )
    # get_file_meta fails (400 / permission error) → pull_file returns skipped_file=True
    failed_result = PullResult(file_key="restricted_key", skipped_file=True)
    mock_pull = AsyncMock(return_value=failed_result)

    with (
        patch.object(FigmaClient, "__new__", return_value=mock_client),
        patch("figmaclaw.commands.pull.pull_file", mock_pull),
    ):
        await _run("key", tmp_path, None, False, None, False, 10, "team123", "all")

    # Manifest should now have the listing's last_modified stamped in
    reloaded = FigmaSyncState(tmp_path)
    reloaded.load()
    assert reloaded.manifest.files["restricted_key"].last_modified == "2026-03-01T00:00:00Z"


@pytest.mark.asyncio
async def test_pull_cmd_forwards_prune_flag_to_pull_file(tmp_path: Path):
    """INVARIANT: pull command forwards prune=False to pull_file when requested."""
    from figmaclaw.commands.pull import _run

    state = _make_state_with_file(tmp_path, "fileA", "")
    state.save()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get_file_meta = AsyncMock(
        return_value={
            "version": "v2",
            "lastModified": "2026-03-01T00:00:00Z",
            "name": "App",
            "document": {"children": []},
        }
    )

    with (
        patch.object(FigmaClient, "__new__", return_value=mock_client),
        patch(
            "figmaclaw.commands.pull.pull_file",
            AsyncMock(return_value=PullResult(file_key="fileA", skipped_file=True)),
        ) as mock_pull,
    ):
        await _run("key", tmp_path, None, False, None, False, 10, None, "all", prune=False)

    assert mock_pull.await_count == 1
    assert mock_pull.await_args is not None
    assert mock_pull.await_args.kwargs["prune"] is False


# --- _compute_raw_frames ---


def test_compute_raw_frames_counts_raw_children_and_ds_names():
    """INVARIANT: _compute_raw_frames classifies INSTANCE children as ds and others as raw."""
    from figmaclaw.pull_logic import _compute_raw_frames

    frame_docs = {
        "11:1": {
            "id": "11:1",
            "type": "FRAME",
            "absoluteBoundingBox": {"x": 0, "y": 0, "width": 400, "height": 300},
            "children": [
                {
                    "id": "11:2",
                    "type": "INSTANCE",
                    "name": "AvatarV2",
                    "componentId": "42:1001",
                    "absoluteBoundingBox": {"x": 0, "y": 0, "width": 40, "height": 40},
                    "children": [],
                },
                {
                    "id": "11:3",
                    "type": "INSTANCE",
                    "name": "ButtonV2",
                    "componentId": "42:1002",
                    "absoluteBoundingBox": {"x": 40, "y": 0, "width": 80, "height": 40},
                    "children": [],
                },
                {
                    "id": "11:4",
                    "type": "FRAME",
                    "name": "raw-child",
                    "absoluteBoundingBox": {"x": 0, "y": 40, "width": 400, "height": 120},
                    "children": [
                        {"id": "11:4:1", "type": "INSTANCE", "name": "CardV2"},
                        {"id": "11:4:2", "type": "RECTANGLE", "name": "bg"},
                    ],
                },
                {
                    "id": "11:5",
                    "type": "TEXT",
                    "name": "label",
                    "absoluteBoundingBox": {"x": 0, "y": 160, "width": 200, "height": 20},
                    "children": [],
                },
            ],
        }
    }
    raw_frames, frame_sections = _compute_raw_frames(frame_docs)
    assert "11:1" in raw_frames
    assert raw_frames["11:1"].raw == 2
    assert raw_frames["11:1"].ds == ["AvatarV2", "ButtonV2"]
    # frame_sections is populated for all frames
    assert "11:1" in frame_sections
    assert len(frame_sections["11:1"]) == 4
    # per-section direct-child inventory exists
    raw_child = next(s for s in frame_sections["11:1"] if s.node_id == "11:4")
    assert raw_child.instances == ["CardV2"]
    assert raw_child.instance_component_ids == []
    assert raw_child.raw_count == 1


def test_compute_raw_frames_omits_fully_componentized_frames_from_raw_frames():
    """INVARIANT: Fully componentized frames are absent from raw_frames but present in frame_sections."""
    from figmaclaw.pull_logic import _compute_raw_frames

    frame_docs = {
        "11:1": {
            "absoluteBoundingBox": {"x": 0, "y": 0, "width": 200, "height": 100},
            "children": [
                {
                    "id": "11:2",
                    "type": "INSTANCE",
                    "name": "AvatarV2",
                    "absoluteBoundingBox": {"x": 0, "y": 0, "width": 40, "height": 40},
                },
                {
                    "id": "11:3",
                    "type": "INSTANCE",
                    "name": "ButtonV2",
                    "absoluteBoundingBox": {"x": 40, "y": 0, "width": 80, "height": 40},
                },
            ],
        }
    }
    raw_frames, frame_sections = _compute_raw_frames(frame_docs)
    assert "11:1" not in raw_frames  # fully componentized — absent signals "clean"
    assert "11:1" in frame_sections  # but still tracked for context building
    assert len(frame_sections["11:1"]) == 2


def test_compute_raw_frames_preserves_ds_duplicates():
    """INVARIANT: ds list preserves duplicates so callers can see N × ButtonV2 instances."""
    from figmaclaw.pull_logic import _compute_raw_frames

    frame_docs = {
        "11:1": {
            "absoluteBoundingBox": {"x": 0, "y": 0, "width": 200, "height": 100},
            "children": [
                {
                    "id": "11:2",
                    "type": "INSTANCE",
                    "name": "ButtonV2",
                    "absoluteBoundingBox": {"x": 0, "y": 0, "width": 80, "height": 40},
                },
                {
                    "id": "11:3",
                    "type": "INSTANCE",
                    "name": "ButtonV2",
                    "absoluteBoundingBox": {"x": 80, "y": 0, "width": 80, "height": 40},
                },
                {
                    "id": "11:4",
                    "type": "RECTANGLE",
                    "name": "bg",
                    "absoluteBoundingBox": {"x": 0, "y": 40, "width": 200, "height": 60},
                },
            ],
        }
    }
    raw_frames, _ = _compute_raw_frames(frame_docs)
    assert raw_frames["11:1"].ds == ["ButtonV2", "ButtonV2"]


def test_compute_raw_frames_returns_empty_for_no_input():
    """INVARIANT: _compute_raw_frames returns empty dicts for empty frame_docs."""
    from figmaclaw.pull_logic import _compute_raw_frames

    raw_frames, frame_sections = _compute_raw_frames({})
    assert raw_frames == {}
    assert frame_sections == {}


def test_compute_raw_frames_handles_non_dict_input_defensively():
    """INVARIANT: _compute_raw_frames returns empty dicts for malformed non-dict input."""
    from figmaclaw.pull_logic import _compute_raw_frames

    raw_frames, frame_sections = _compute_raw_frames(None)  # type: ignore[arg-type]
    assert raw_frames == {}
    assert frame_sections == {}


def test_compute_raw_frames_section_positions_are_relative_to_frame():
    """INVARIANT: SectionNode positions are relative to the parent frame, not absolute canvas coords."""
    from figmaclaw.pull_logic import _compute_raw_frames

    frame_docs = {
        "11:1": {
            "absoluteBoundingBox": {"x": 100, "y": 200, "width": 393, "height": 300},
            "children": [
                {
                    "id": "11:2",
                    "type": "FRAME",
                    "name": "Header",
                    "absoluteBoundingBox": {"x": 100, "y": 200, "width": 393, "height": 60},
                },
                {
                    "id": "11:3",
                    "type": "FRAME",
                    "name": "Content",
                    "absoluteBoundingBox": {"x": 116, "y": 260, "width": 361, "height": 240},
                },
            ],
        }
    }
    _, frame_sections = _compute_raw_frames(frame_docs)
    sections = frame_sections["11:1"]
    header = next(s for s in sections if s.name == "Header")
    content = next(s for s in sections if s.name == "Content")
    assert header.x == 0 and header.y == 0
    assert content.x == 16 and content.y == 60  # relative to frame origin


def test_compute_raw_frames_section_inventory_counts_direct_children():
    """INVARIANT: frame_sections entries include direct-child instance list and raw_count."""
    from figmaclaw.pull_logic import _compute_raw_frames

    frame_docs = {
        "11:1": {
            "absoluteBoundingBox": {"x": 100, "y": 200, "width": 393, "height": 300},
            "children": [
                {
                    "id": "11:2",
                    "type": "FRAME",
                    "name": "Header",
                    "absoluteBoundingBox": {"x": 100, "y": 200, "width": 393, "height": 60},
                    "children": [
                        {"id": "11:2:1", "type": "INSTANCE", "name": "IconStat"},
                        {
                            "id": "11:2:2",
                            "type": "INSTANCE",
                            "name": "IconStat",
                            "componentId": "55:3001",
                        },
                        {
                            "id": "11:2:3",
                            "type": "INSTANCE",
                            "name": "IconStat",
                            "componentId": "55:3001",
                        },
                        {"id": "11:2:4", "type": "TEXT", "name": "Title"},
                    ],
                }
            ],
        }
    }
    _, frame_sections = _compute_raw_frames(frame_docs)
    section = frame_sections["11:1"][0]
    assert section.instances == ["IconStat", "IconStat", "IconStat"]
    assert section.instance_component_ids == ["55:3001", "55:3001"]
    assert section.raw_count == 1


# --- _build_component_set_keys ---


def test_build_component_set_keys_matches_by_page_id():
    """INVARIANT: _build_component_set_keys returns name→key for component sets on the same page.

    The /component_sets API returns published component sets with containing_frame.pageId.
    Matching is by pageId because published sets are page-level nodes — not inside sections.
    Private locked component sets inside sections are not returned by the Figma API.
    """
    from figmaclaw.pull_logic import _build_component_set_keys

    component_sets = [
        {"containing_frame": {"pageId": "449:42"}, "name": "avatar", "key": "aabb1122"},
        {"containing_frame": {"pageId": "783:9631"}, "name": "button", "key": "ccdd3344"},
    ]
    result = _build_component_set_keys("449:42", component_sets)
    assert result == {"avatar": "aabb1122"}
    assert "button" not in result


def test_build_component_set_keys_returns_empty_when_no_match():
    """INVARIANT: _build_component_set_keys returns {} when no component sets are on this page."""
    from figmaclaw.pull_logic import _build_component_set_keys

    result = _build_component_set_keys("449:42", [])
    assert result == {}


# --- pull_file: new API calls and frontmatter fields ---


@pytest.mark.asyncio
async def test_pull_file_calls_get_component_sets_once_per_changed_file(pull_env: PullEnv):
    """INVARIANT: pull_file calls get_component_sets exactly once per changed file."""
    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    mock_client.get_component_sets.assert_called_once_with("abc123")


@pytest.mark.asyncio
async def test_pull_file_does_not_call_get_component_sets_when_file_unchanged(tmp_path: Path):
    """INVARIANT: pull_file skips get_component_sets when the file version is unchanged and schema is current."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files["abc123"].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_component_sets = AsyncMock(return_value=[])

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    mock_client.get_component_sets.assert_not_called()


@pytest.mark.asyncio
async def test_pull_file_calls_get_nodes_for_changed_screen_page(pull_env: PullEnv):
    """INVARIANT: pull_file calls get_nodes once for each changed page that has screen frames."""
    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_page_node_with_children())
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    mock_client.get_nodes.assert_called_once()
    call_args = mock_client.get_nodes.call_args
    assert call_args.args[0] == "abc123"  # file_key
    assert "11:1" in call_args.args[1]  # frame node ID included


@pytest.mark.asyncio
async def test_pull_file_writes_raw_frames_to_new_page_frontmatter(pull_env: PullEnv):
    """INVARIANT: raw_frames from get_nodes appears in written page frontmatter."""
    from figmaclaw.figma_parse import parse_frontmatter

    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_page_node_with_children())
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    out = tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding-7741-45837.md"
    fm = parse_frontmatter(out.read_text())
    assert fm is not None
    assert "11:1" in fm.raw_frames
    assert fm.raw_frames["11:1"].raw == 2  # RECTANGLE + TEXT
    assert fm.raw_frames["11:1"].ds == ["AvatarV2"]
    # frame_sections now carries per-section inventory needed by #38
    assert "11:1" in fm.frame_sections
    first = fm.frame_sections["11:1"][0]
    assert first.instances == []
    assert first.instance_component_ids == []
    assert first.raw_count == 0


@pytest.mark.asyncio
async def test_pull_file_is_idempotent_for_frame_sections_inventory(pull_env: PullEnv):
    """INVARIANT: repeated unchanged pulls preserve frame_sections inventory byte-for-byte."""
    from figmaclaw.figma_parse import parse_frontmatter

    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_page_node_with_children())
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    out = tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding-7741-45837.md"
    first_text = out.read_text()
    fm1 = parse_frontmatter(first_text)
    assert fm1 is not None
    assert "11:1" in fm1.frame_sections

    # Force a second run (same content) to assert frontmatter stability.
    await pull_file(mock_client, "abc123", state, tmp_path, force=True)
    second_text = out.read_text()
    fm2 = parse_frontmatter(second_text)
    assert fm2 is not None
    assert fm2.frame_sections == fm1.frame_sections
    assert first_text == second_text


@pytest.mark.asyncio
async def test_pull_file_writes_component_set_keys_to_component_frontmatter(pull_env: PullEnv):
    """INVARIANT: component_set_keys from get_component_sets appears in component .md frontmatter."""
    from figmaclaw.figma_parse import parse_frontmatter

    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path

    # Matching is by containing_frame.pageId, not by frame node IDs.
    # The page node ID for fake_component_page_node is "7741:45837".
    component_sets = [
        {
            "containing_frame": {"pageId": "7741:45837"},
            "name": "ButtonV2",
            "key": "aabb1122cc334455",
        },
        {
            "containing_frame": {"pageId": "7741:45837"},
            "name": "ButtonOutline",
            "key": "ddeeff0011223344",
        },
        {"containing_frame": {"pageId": "9999:1"}, "name": "OtherFile", "key": "unrelated"},
    ]
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())
    mock_client.get_component_sets = AsyncMock(return_value=component_sets)

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    comp_out = tmp_path / "figma" / "web-app-abc123" / "components" / "buttons-20-1.md"
    fm = parse_frontmatter(comp_out.read_text())
    assert fm is not None
    assert fm.component_set_keys == {
        "ButtonV2": "aabb1122cc334455",
        "ButtonOutline": "ddeeff0011223344",
    }
    assert "OtherFile" not in fm.component_set_keys


@pytest.mark.asyncio
async def test_pull_file_handles_get_component_sets_failure_gracefully(pull_env: PullEnv):
    """INVARIANT: pull_file continues and omits component_set_keys when get_component_sets fails."""
    from figmaclaw.figma_parse import parse_frontmatter

    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node())
    mock_client.get_component_sets = AsyncMock(side_effect=Exception("API error"))
    mock_client.get_nodes = AsyncMock(return_value={})

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.component_sections_written == 1  # still wrote the file
    comp_out = tmp_path / "figma" / "web-app-abc123" / "components" / "buttons-20-1.md"
    fm = parse_frontmatter(comp_out.read_text())
    assert fm is not None
    assert fm.component_set_keys == {}  # empty — not an error


@pytest.mark.asyncio
async def test_pull_file_handles_get_nodes_failure_gracefully(pull_env: PullEnv):
    """INVARIANT: pull_file continues and omits raw_frames when get_nodes fails."""
    from figmaclaw.figma_parse import parse_frontmatter

    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path
    mock_client.get_page = AsyncMock(return_value=fake_page_node_with_children())
    mock_client.get_nodes = AsyncMock(side_effect=Exception("timeout"))

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.pages_written == 1  # still wrote the page
    out = tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding-7741-45837.md"
    fm = parse_frontmatter(out.read_text())
    assert fm is not None
    assert fm.raw_frames == {}  # absent from frontmatter when fetch failed


# --- Pull schema version (staleness bypass) ---


@pytest.mark.asyncio
async def test_pull_file_writes_pull_schema_version_to_manifest_after_success(pull_env: PullEnv):
    """INVARIANT: pull_schema_version is written to the manifest after all pages complete."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION

    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path

    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert state.manifest.files["abc123"].pull_schema_version == CURRENT_PULL_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_pull_file_does_not_write_pull_schema_version_when_has_more(pull_env: PullEnv):
    """INVARIANT: pull_schema_version is NOT written when max_pages was hit (partial run)."""
    state, mock_client, tmp_path = pull_env.state, pull_env.client, pull_env.tmp_path

    # With max_pages=0 the loop exits immediately without writing any pages
    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False, max_pages=0)

    assert result.has_more is True
    assert state.manifest.files["abc123"].pull_schema_version == 0  # not bumped


@pytest.mark.asyncio
async def test_pull_file_processes_schema_stale_file_even_when_figma_unchanged(tmp_path: Path):
    """INVARIANT: when pull_schema_version < CURRENT, file is re-processed even if Figma version matches."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    # Figma version matches — would normally be skipped
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"
    # But pull_schema_version is behind current (default 0)
    assert state.manifest.files["abc123"].pull_schema_version < CURRENT_PULL_SCHEMA_VERSION

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=fake_page_node())
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value={})

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    # File was NOT skipped despite matching Figma version — schema staleness triggered processing
    assert result.skipped_file is False
    assert result.pages_written == 1


@pytest.mark.asyncio
async def test_pull_file_processes_schema_stale_pages_even_when_page_hash_unchanged(tmp_path: Path):
    """INVARIANT: schema_stale upgrades existing pages even when page hash is unchanged.

    Schema-only upgrades (hash unchanged) go to pages_schema_upgraded, NOT pages_written.
    They don't consume the max_pages budget so the upgrade always completes in a single pass.
    """
    from figmaclaw.figma_parse import parse_frontmatter

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    page_node = fake_page_node_with_children()
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    # First pull: write the page, bump version and schema version
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    out = tmp_path / "figma" / "web-app-abc123" / "pages" / "onboarding-7741-45837.md"
    body_before = out.read_text().split("---\n", 2)[-1]  # capture body
    fm_before = parse_frontmatter(out.read_text())
    assert fm_before is not None
    assert "11:1" in fm_before.frame_sections
    first_before = fm_before.frame_sections["11:1"][0]
    assert hasattr(first_before, "instance_component_ids")

    # Simulate schema staleness: reset pull_schema_version to 0 (pre-versioning)
    state.manifest.files["abc123"].pull_schema_version = 0
    # Also update Figma version so file passes file-level check... wait, we need schema_stale
    # Actually with version unchanged AND schema stale, it should still process
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"
    state.save()

    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))

    get_nodes_calls_before = mock_client.get_nodes.await_count
    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    # Schema-only upgrade: page hash unchanged, goes to pages_schema_upgraded (not pages_written)
    assert result.pages_written == 0
    assert result.pages_schema_upgraded == 1
    # Schema-only upgrades never set has_more=True even with max_pages limits
    assert result.has_more is False

    # Body must be preserved — only frontmatter changed
    body_after = out.read_text().split("---\n", 2)[-1]
    assert body_before == body_after
    fm_after = parse_frontmatter(out.read_text())
    assert fm_after is not None
    assert "11:1" in fm_after.frame_sections
    first_after = fm_after.frame_sections["11:1"][0]
    # v6 schema upgrade must backfill stable identifiers, not only legacy names/raw_count.
    assert hasattr(first_after, "instance_component_ids")
    # Parallel schema-only pass must still fetch frame docs for unchanged pages.
    assert mock_client.get_nodes.await_count > get_nodes_calls_before


@pytest.mark.asyncio
async def test_pull_file_skips_file_when_schema_current_and_figma_unchanged(tmp_path: Path):
    """INVARIANT: when pull_schema_version == CURRENT and Figma unchanged, file is skipped."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files["abc123"].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.skipped_file is True
    mock_client.get_page.assert_not_called()


@pytest.mark.asyncio
async def test_schema_upgrade_does_not_cause_infinite_loop_with_max_pages(tmp_path: Path):
    """INVARIANT: schema-stale files converge in one pass regardless of max_pages.

    When pull_schema_version < CURRENT_PULL_SCHEMA_VERSION, pages with unchanged hashes
    are treated as schema-only upgrades that don't consume the max_pages budget.
    This prevents the infinite loop where the same N pages are reprocessed every batch
    because has_more=True always prevents pull_schema_version from being updated.
    """
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")

    mock_client = MagicMock(spec=FigmaClient)
    page_node = fake_page_node_with_children()
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    # First pull: write the page, schema version set to CURRENT
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    assert state.manifest.files["abc123"].pull_schema_version == CURRENT_PULL_SCHEMA_VERSION

    # Simulate schema bump: reset pull_schema_version so it appears stale
    state.manifest.files["abc123"].pull_schema_version = 0
    state.save()

    # Second pull with max_pages=1 — schema-only upgrades must NOT consume the budget.
    # Before the fix, schema_stale bypassed the hash check → pages_written=1 → has_more=True
    # → pull_schema_version never reached CURRENT → infinite loop every batch.
    result = await pull_file(
        mock_client,
        "abc123",
        state,
        tmp_path,
        force=False,
        max_pages=1,
    )

    # Schema-only: no content changes consumed the budget, no has_more
    assert result.pages_written == 0
    assert result.pages_schema_upgraded >= 1
    assert result.has_more is False

    # pull_schema_version must be updated after a schema-only pass
    assert state.manifest.files["abc123"].pull_schema_version == CURRENT_PULL_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_pull_file_sets_no_access_on_http_400(tmp_path: Path):
    """INVARIANT: pull_file returns no_access=True when get_file_meta raises HTTP 400."""
    import httpx

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")

    mock_client = MagicMock(spec=FigmaClient)
    response_mock = MagicMock()
    response_mock.status_code = 400
    mock_client.get_file_meta = AsyncMock(
        side_effect=httpx.HTTPStatusError("400", request=MagicMock(), response=response_mock)
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.no_access is True


@pytest.mark.asyncio
async def test_pull_file_sets_no_access_on_http_404(tmp_path: Path):
    """INVARIANT: pull_file returns no_access=True when get_file_meta raises HTTP 404."""
    import httpx

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")

    mock_client = MagicMock(spec=FigmaClient)
    response_mock = MagicMock()
    response_mock.status_code = 404
    mock_client.get_file_meta = AsyncMock(
        side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=response_mock)
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.no_access is True


def _custom_file_meta(
    *,
    version: str,
    file_name: str,
    page_id: str,
    page_name: str,
) -> FileMetaResponse:
    return FileMetaResponse.model_validate(
        {
            "version": version,
            "lastModified": "2026-03-31T12:00:00Z",
            "name": file_name,
            "document": {"children": [{"id": page_id, "name": page_name, "type": "CANVAS"}]},
        }
    )


@pytest.mark.asyncio
async def test_pull_file_is_idempotent_second_call_changes_nothing(tmp_path: Path):
    """INVARIANT: a second pull on identical Figma content must not modify any file on disk.

    This catches any write operation that ignores content equality (e.g. always
    writing generated_at timestamps, always rewriting frontmatter even when unchanged).
    Any such violation would produce spurious git commits and waste CI resources.
    """
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    page_node = fake_page_node_with_children()
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-01-01T00:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    # First pull: writes the page and tokens sidecar
    result1 = await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    assert result1.pages_written == 1

    # Capture all file contents after first pull
    all_files_before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }

    # Second pull: same Figma version, same content — no file should change
    result2 = await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    assert result2.pages_written == 0
    assert (
        result2.skipped_file is True
    )  # file-level skip fires when version/last_modified unchanged

    all_files_after = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }

    assert all_files_before == all_files_after, (
        "Files changed on second pull despite identical Figma content: "
        + str({k for k in all_files_after if all_files_after[k] != all_files_before.get(k)})
    )


@pytest.mark.asyncio
async def test_pull_file_backfills_missing_sidecar_on_unchanged_page(tmp_path: Path):
    """INVARIANT: unchanged pages with missing sidecar are re-processed to recreate sidecar."""
    from figmaclaw.figma_hash import compute_page_hash

    page_id = "7741:45837"
    file_key = "abc123"
    page_node = fake_page_node_with_children()
    page_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(file_key, "Web App")
    state.manifest.files[file_key].version = "v2"
    state.manifest.files[file_key].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files[file_key].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION
    md_rel = "figma/web-app-abc123/pages/onboarding-7741-45837.md"
    state.manifest.files[file_key].pages[page_id] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding-7741-45837",
        md_path=md_rel,
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T12:00:00Z",
    )
    state.save()

    md_abs = tmp_path / md_rel
    md_abs.parent.mkdir(parents=True, exist_ok=True)
    md_abs.write_text(
        "---\nfile_key: abc123\npage_node_id: '7741:45837'\nframes: ['11:1']\n---\n\n# body\n"
    )
    assert not md_abs.with_suffix(".tokens.json").exists()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    result = await pull_file(mock_client, file_key, state, tmp_path, force=False)

    assert result.skipped_file is False
    assert md_abs.with_suffix(".tokens.json").exists()


@pytest.mark.asyncio
async def test_pull_file_migrates_legacy_sidecar_on_unchanged_page(tmp_path: Path):
    """INVARIANT: unchanged pages with legacy sidecar schema are re-processed to v2."""
    from figmaclaw.figma_hash import compute_page_hash

    page_id = "7741:45837"
    file_key = "abc123"
    page_node = fake_page_node_with_children()
    page_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(file_key, "Web App")
    state.manifest.files[file_key].version = "v2"
    state.manifest.files[file_key].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files[file_key].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION
    md_rel = "figma/web-app-abc123/pages/onboarding-7741-45837.md"
    state.manifest.files[file_key].pages[page_id] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding-7741-45837",
        md_path=md_rel,
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T12:00:00Z",
    )
    state.save()

    md_abs = tmp_path / md_rel
    md_abs.parent.mkdir(parents=True, exist_ok=True)
    md_abs.write_text(
        "---\nfile_key: abc123\npage_node_id: '7741:45837'\nframes: ['11:1']\n---\n\n# body\n"
    )
    sidecar = md_abs.with_suffix(".tokens.json")
    sidecar.write_text(
        '{"file_key":"abc123","page_node_id":"7741:45837","summary":{"raw":1,"stale":0,"valid":0},"frames":{}}'
    )

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    result = await pull_file(mock_client, file_key, state, tmp_path, force=False)

    assert result.skipped_file is False
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text())
    assert payload["schema_version"] == 2


@pytest.mark.asyncio
async def test_pull_file_backfills_missing_sidecar_on_unchanged_page_when_schema_stale(
    tmp_path: Path,
):
    """INVARIANT: sidecar backfill still happens during schema-only upgrade runs."""
    from figmaclaw.figma_hash import compute_page_hash

    page_id = "7741:45837"
    file_key = "abc123"
    page_node = fake_page_node_with_children()
    page_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(file_key, "Web App")
    state.manifest.files[file_key].version = "v2"
    state.manifest.files[file_key].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files[file_key].pull_schema_version = max(0, CURRENT_PULL_SCHEMA_VERSION - 1)
    md_rel = "figma/web-app-abc123/pages/onboarding-7741-45837.md"
    state.manifest.files[file_key].pages[page_id] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding-7741-45837",
        md_path=md_rel,
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T12:00:00Z",
    )
    state.save()

    md_abs = tmp_path / md_rel
    md_abs.parent.mkdir(parents=True, exist_ok=True)
    md_abs.write_text(
        "---\nfile_key: abc123\npage_node_id: '7741:45837'\nframes: ['11:1']\n---\n\n# body\n"
    )
    assert not md_abs.with_suffix(".tokens.json").exists()

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    result = await pull_file(mock_client, file_key, state, tmp_path, force=False)

    assert result.skipped_file is False
    assert result.pages_schema_upgraded == 1
    assert md_abs.with_suffix(".tokens.json").exists()


@pytest.mark.asyncio
async def test_pull_file_migrates_legacy_sidecar_on_unchanged_page_when_schema_stale(
    tmp_path: Path,
):
    """INVARIANT: legacy sidecar schema migrates to v2 even during schema-only upgrades."""
    from figmaclaw.figma_hash import compute_page_hash

    page_id = "7741:45837"
    file_key = "abc123"
    page_node = fake_page_node_with_children()
    page_hash = compute_page_hash(page_node)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(file_key, "Web App")
    state.manifest.files[file_key].version = "v2"
    state.manifest.files[file_key].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files[file_key].pull_schema_version = max(0, CURRENT_PULL_SCHEMA_VERSION - 1)
    md_rel = "figma/web-app-abc123/pages/onboarding-7741-45837.md"
    state.manifest.files[file_key].pages[page_id] = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding-7741-45837",
        md_path=md_rel,
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T12:00:00Z",
    )
    state.save()

    md_abs = tmp_path / md_rel
    md_abs.parent.mkdir(parents=True, exist_ok=True)
    md_abs.write_text(
        "---\nfile_key: abc123\npage_node_id: '7741:45837'\nframes: ['11:1']\n---\n\n# body\n"
    )
    sidecar = md_abs.with_suffix(".tokens.json")
    sidecar.write_text(
        '{"file_key":"abc123","page_node_id":"7741:45837","summary":{"raw":1,"stale":0,"valid":0},"frames":{}}'
    )

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    result = await pull_file(mock_client, file_key, state, tmp_path, force=False)

    assert result.skipped_file is False
    assert result.pages_schema_upgraded == 1
    payload = json.loads(sidecar.read_text())
    assert payload["schema_version"] == 2


@pytest.mark.asyncio
async def test_pull_file_page_rename_moves_path_and_prunes_old(tmp_path: Path):
    """INVARIANT: renaming a page keeps exactly one page file path (old path is pruned)."""
    page_id = "100:1"
    old_page_name = "Showcase LSN"
    new_page_name = "Showcase V2"
    file_name = "Web App"

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", file_name)
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value={})

    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v2", file_name=file_name, page_id=page_id, page_name=old_page_name
        )
    )
    mock_client.get_page = AsyncMock(return_value=fake_page_node_for_id(page_id, old_page_name))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    old_rel = page_path(
        f"{slugify(file_name)}-abc123", f"{slugify(old_page_name)}-{page_id.replace(':', '-')}"
    )
    old_abs = tmp_path / old_rel
    assert old_abs.exists()
    old_abs.write_text(old_abs.read_text() + "\nMANUAL_BODY_SENTINEL\n")

    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v3", file_name=file_name, page_id=page_id, page_name=new_page_name
        )
    )
    mock_client.get_page = AsyncMock(return_value=fake_page_node_for_id(page_id, new_page_name))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    new_rel = page_path(
        f"{slugify(file_name)}-abc123", f"{slugify(new_page_name)}-{page_id.replace(':', '-')}"
    )
    new_abs = tmp_path / new_rel
    assert new_abs.exists()
    assert not old_abs.exists()
    assert "MANUAL_BODY_SENTINEL" in new_abs.read_text()

    files_before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    files_after = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    assert files_before == files_after


@pytest.mark.asyncio
async def test_pull_file_file_rename_moves_path_and_prunes_old(tmp_path: Path):
    """INVARIANT: renaming a file (same key) moves generated page paths to the new file slug."""
    page_id = "100:1"
    page_name = "Reactions"
    old_file_name = "Mobile Streaming Interface"
    new_file_name = "Web Streaming Interface"

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", old_file_name)
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value={})

    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v2", file_name=old_file_name, page_id=page_id, page_name=page_name
        )
    )
    mock_client.get_page = AsyncMock(return_value=fake_page_node_for_id(page_id, page_name))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    old_rel = page_path(
        f"{slugify(old_file_name)}-abc123", f"{slugify(page_name)}-{page_id.replace(':', '-')}"
    )
    old_abs = tmp_path / old_rel
    assert old_abs.exists()

    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v3", file_name=new_file_name, page_id=page_id, page_name=page_name
        )
    )
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    new_rel = page_path(
        f"{slugify(new_file_name)}-abc123", f"{slugify(page_name)}-{page_id.replace(':', '-')}"
    )
    new_abs = tmp_path / new_rel
    assert new_abs.exists()
    assert not old_abs.exists()


@pytest.mark.asyncio
async def test_pull_file_page_rename_with_prune_disabled_keeps_old_path(tmp_path: Path):
    """INVARIANT: with prune=False, rename writes new path but keeps old generated path."""
    page_id = "100:1"
    old_page_name = "Showcase LSN"
    new_page_name = "Showcase V2"
    file_name = "Web App"

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", file_name)
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value={})

    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v2", file_name=file_name, page_id=page_id, page_name=old_page_name
        )
    )
    mock_client.get_page = AsyncMock(return_value=fake_page_node_for_id(page_id, old_page_name))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    old_rel = page_path(
        f"{slugify(file_name)}-abc123", f"{slugify(old_page_name)}-{page_id.replace(':', '-')}"
    )
    old_abs = tmp_path / old_rel
    assert old_abs.exists()

    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v3", file_name=file_name, page_id=page_id, page_name=new_page_name
        )
    )
    mock_client.get_page = AsyncMock(return_value=fake_page_node_for_id(page_id, new_page_name))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False, prune=False)

    new_rel = page_path(
        f"{slugify(file_name)}-abc123", f"{slugify(new_page_name)}-{page_id.replace(':', '-')}"
    )
    new_abs = tmp_path / new_rel
    assert new_abs.exists()
    assert old_abs.exists()


@pytest.mark.asyncio
async def test_pull_file_unchanged_run_prunes_existing_generated_orphans(tmp_path: Path):
    """INVARIANT: unchanged file-level skip still prunes generated orphan md/tokens files."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v2"
    state.manifest.files["abc123"].last_modified = "2026-03-31T12:00:00Z"
    state.manifest.files["abc123"].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION
    state.manifest.files["abc123"].pages["100:1"] = PageEntry(
        page_name="Current",
        page_slug="current-100-1",
        md_path="figma/web-app-abc123/pages/current-100-1.md",
        page_hash="hash",
        last_refreshed_at="now",
    )
    state.save()

    current_md = tmp_path / "figma/web-app-abc123/pages/current-100-1.md"
    current_md.parent.mkdir(parents=True, exist_ok=True)
    current_md.write_text("current")
    current_md.with_suffix(".tokens.json").write_text('{"schema_version":2}')

    orphan_md = tmp_path / "figma/web-app-abc123/pages/legacy-100-99.md"
    orphan_md.write_text("orphan")
    orphan_tok = orphan_md.with_suffix(".tokens.json")
    orphan_tok.write_text("{}")

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert result.skipped_file is True
    assert current_md.exists()
    assert not orphan_md.exists()
    assert not orphan_tok.exists()


@pytest.mark.asyncio
async def test_pull_file_unchanged_skip_does_not_prune_other_file_paths_in_candidate_dir(
    tmp_path: Path,
):
    """INVARIANT: prune on one file never deletes files referenced by another tracked file."""
    file_a = "fileA1111111111111111111111"
    file_b = "fileB2222222222222222222222"

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(file_a, "Web App")
    state.add_tracked_file(file_b, "Design System")

    for key in (file_a, file_b):
        state.manifest.files[key].version = "v2"
        state.manifest.files[key].last_modified = "2026-03-31T12:00:00Z"
        state.manifest.files[key].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION

    state.manifest.files[file_a].pages["100:1"] = PageEntry(
        page_name="Alpha",
        page_slug="alpha-100-1",
        md_path="figma/web-app-fileA1111111111111111111111/pages/alpha-100-1.md",
        page_hash="hash-a",
        last_refreshed_at="now",
    )
    state.manifest.files[file_b].pages["200:1"] = PageEntry(
        page_name="Beta",
        page_slug="beta-200-1",
        md_path="figma/design-system-fileB2222222222222222222222/pages/beta-200-1.md",
        page_hash="hash-b",
        last_refreshed_at="now",
    )
    state.save()

    alpha = tmp_path / "figma/web-app-fileA1111111111111111111111/pages/alpha-100-1.md"
    beta = tmp_path / "figma/design-system-fileB2222222222222222222222/pages/beta-200-1.md"
    alpha.parent.mkdir(parents=True, exist_ok=True)
    beta.parent.mkdir(parents=True, exist_ok=True)
    alpha.write_text("alpha")
    beta.write_text("beta")
    alpha.with_suffix(".tokens.json").write_text('{"schema_version":2}')
    beta.with_suffix(".tokens.json").write_text('{"schema_version":2}')

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-03-31T12:00:00Z"))

    result = await pull_file(mock_client, file_a, state, tmp_path, force=False)
    assert result.skipped_file is True
    assert alpha.exists()
    assert beta.exists()


@pytest.mark.asyncio
async def test_pull_file_screen_to_component_only_prunes_old_screen_md_and_sidecar(tmp_path: Path):
    """INVARIANT: screen->component-only transition removes old page md + tokens sidecar."""
    page_id = "100:1"
    file_name = "Web App"

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", file_name)
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value={})

    # First run: screen page exists
    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v2",
            file_name=file_name,
            page_id=page_id,
            page_name="Catalog",
        )
    )
    mock_client.get_page = AsyncMock(return_value=fake_page_node_for_id(page_id, "Catalog"))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    old_rel = page_path(f"{slugify(file_name)}-abc123", f"catalog-{page_id.replace(':', '-')}")
    old_abs = tmp_path / old_rel
    assert old_abs.exists()
    old_sidecar = old_abs.with_suffix(".tokens.json")
    old_sidecar.write_text("{}")

    # Second run: same page id becomes component-only
    mock_client.get_file_meta = AsyncMock(
        return_value=_custom_file_meta(
            version="v3",
            file_name=file_name,
            page_id=page_id,
            page_name="Catalog",
        )
    )
    mock_client.get_page = AsyncMock(return_value=fake_component_page_node(page_id))
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    assert not old_abs.exists()
    assert not old_sidecar.exists()

    entry = state.manifest.files["abc123"].pages[page_id]
    assert entry.md_path is None
    assert entry.component_md_paths


@pytest.mark.asyncio
async def test_pull_file_removed_page_prunes_manifest_and_files(tmp_path: Path):
    """INVARIANT: pages removed from file metadata are pruned from manifest and disk."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"
    state.manifest.files["abc123"].pages["100:1"] = PageEntry(
        page_name="Legacy",
        page_slug="legacy-100-1",
        md_path="figma/web-app-abc123/pages/legacy-100-1.md",
        page_hash="oldhash",
        last_refreshed_at="2026-03-01T00:00:00Z",
        component_md_paths=["figma/web-app-abc123/components/legacy-components-100-2.md"],
    )
    state.save()

    page_md = tmp_path / "figma/web-app-abc123/pages/legacy-100-1.md"
    page_md.parent.mkdir(parents=True, exist_ok=True)
    page_md.write_text("legacy")
    page_sidecar = page_md.with_suffix(".tokens.json")
    page_sidecar.write_text("{}")
    comp_md = tmp_path / "figma/web-app-abc123/components/legacy-components-100-2.md"
    comp_md.parent.mkdir(parents=True, exist_ok=True)
    comp_md.write_text("legacy-comp")

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value={})
    mock_client.get_file_meta = AsyncMock(
        return_value=FileMetaResponse.model_validate(
            {
                "version": "v2",
                "lastModified": "2026-03-31T12:00:00Z",
                "name": "Web App",
                "document": {"children": []},
            }
        )
    )

    result = await pull_file(mock_client, "abc123", state, tmp_path, force=False)
    assert result.pages_written == 0
    assert not page_md.exists()
    assert not page_sidecar.exists()
    assert not comp_md.exists()
    assert "100:1" not in state.manifest.files["abc123"].pages


@pytest.mark.asyncio
async def test_has_more_only_set_when_content_changes_exhaust_budget(tmp_path: Path):
    """INVARIANT: has_more=True requires content-changed pages, not just schema upgrades.

    Before the fix, schema_stale caused has_more=True even when all pages had unchanged
    hashes. This prevented pull_schema_version from ever reaching CURRENT_PULL_SCHEMA_VERSION.
    Schema-only upgrades must NOT consume the max_pages budget.
    """
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].version = "v1"

    mock_client = MagicMock(spec=FigmaClient)
    page_node = fake_page_node_with_children()
    mock_client.get_file_meta = AsyncMock(return_value=fake_file_meta("v2", "2026-01-01T00:00:00Z"))
    mock_client.get_page = AsyncMock(return_value=page_node)
    mock_client.get_component_sets = AsyncMock(return_value=[])
    mock_client.get_nodes = AsyncMock(return_value=fake_get_nodes_response())

    # First pull: establishes page hash and schema version
    await pull_file(mock_client, "abc123", state, tmp_path, force=False)

    # Simulate schema bump by resetting pull_schema_version
    state.manifest.files["abc123"].pull_schema_version = 0
    state.save()

    # Schema-stale pull with max_pages=1: schema-only pages must not trigger has_more
    result = await pull_file(
        mock_client,
        "abc123",
        state,
        tmp_path,
        force=False,
        max_pages=1,
    )

    assert result.has_more is False, (
        "has_more=True on a schema-only upgrade — schema upgrades must not consume "
        "the max_pages budget (would cause an infinite loop in the CI batch loop)"
    )
    assert result.pages_written == 0
    assert result.pages_schema_upgraded >= 1
    assert result.skipped_file is False  # schema_stale=True bypasses file-level skip


@pytest.mark.asyncio
async def test_pull_cmd_no_access_prunes_artifacts_and_untracks(tmp_path: Path):
    """INVARIANT: pull command prunes known artifacts and untracks file on no_access."""
    from figmaclaw.commands.pull import _run

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("restricted_key", "Restricted")
    state.manifest.files["restricted_key"].pages["11:1"] = PageEntry(
        page_name="Legacy",
        page_slug="legacy-11-1",
        md_path="figma/restricted/pages/legacy-11-1.md",
        page_hash="hash",
        last_refreshed_at="now",
        component_md_paths=["figma/restricted/components/legacy-components-11-2.md"],
    )
    state.save()

    page_md = tmp_path / "figma/restricted/pages/legacy-11-1.md"
    page_md.parent.mkdir(parents=True, exist_ok=True)
    page_md.write_text("legacy")
    sidecar = page_md.with_suffix(".tokens.json")
    sidecar.write_text("{}")
    comp_md = tmp_path / "figma/restricted/components/legacy-components-11-2.md"
    comp_md.parent.mkdir(parents=True, exist_ok=True)
    comp_md.write_text("legacy-comp")

    mock_client = MagicMock(spec=FigmaClient)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    no_access_result = PullResult(file_key="restricted_key", no_access=True, skipped_file=True)
    with (
        patch.object(FigmaClient, "__new__", return_value=mock_client),
        patch("figmaclaw.commands.pull.pull_file", AsyncMock(return_value=no_access_result)),
    ):
        await _run("key", tmp_path, None, False, None, False, 10, None, "all")

    assert not page_md.exists()
    assert not sidecar.exists()
    assert not comp_md.exists()

    reloaded = FigmaSyncState(tmp_path)
    reloaded.load()
    assert "restricted_key" not in reloaded.manifest.tracked_files
    assert "restricted_key" not in reloaded.manifest.files
    assert (
        reloaded.manifest.skipped_files["restricted_key"]
        == "no access — get_file_meta returns 400/404"
    )
