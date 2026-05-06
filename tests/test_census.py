"""Tests for figmaclaw census idempotency and hash correctness.

INVARIANTS:
- _compute_hash is deterministic: same component sets → same hash regardless of input order
- _compute_hash changes when the registry changes (add / remove / rename)
- _render embeds a content_hash that _existing_hash can extract (round-trip)
  If this round-trip breaks, the skip check fires on every run → spurious commits
- census skips the write when content_hash is unchanged
- census writes when the component registry changes
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import figmaclaw.commands.census as census_module
from figmaclaw.commands.census import _compute_hash, _existing_hash, _render, _run
from figmaclaw.figma_paths import file_slug_for_key
from figmaclaw.figma_sync_state import FigmaSyncState


def _make_component_set(name: str, key: str, page: str = "Components") -> dict:
    return {
        "name": name,
        "key": key,
        "containing_frame": {"pageName": page},
        "updated_at": "2026-01-01T00:00:00Z",
    }


class TestComputeHash:
    def test_same_input_produces_same_hash(self):
        """INVARIANT: _compute_hash is deterministic — same input always gives the same hash."""
        cs = [_make_component_set("Button", "aabb1122")]
        assert _compute_hash(cs) == _compute_hash(cs)

    def test_order_independent(self):
        """INVARIANT: _compute_hash ignores input ordering — only registry membership matters."""
        cs1 = [_make_component_set("Button", "aa"), _make_component_set("Input", "bb")]
        cs2 = [_make_component_set("Input", "bb"), _make_component_set("Button", "aa")]
        assert _compute_hash(cs1) == _compute_hash(cs2)

    def test_changes_when_component_added(self):
        """INVARIANT: adding a component set changes the hash."""
        cs_before = [_make_component_set("Button", "aa")]
        cs_after = [_make_component_set("Button", "aa"), _make_component_set("Input", "bb")]
        assert _compute_hash(cs_before) != _compute_hash(cs_after)

    def test_changes_when_component_removed(self):
        """INVARIANT: removing a component set changes the hash."""
        cs_before = [_make_component_set("Button", "aa"), _make_component_set("Input", "bb")]
        cs_after = [_make_component_set("Button", "aa")]
        assert _compute_hash(cs_before) != _compute_hash(cs_after)

    def test_changes_when_component_renamed(self):
        """INVARIANT: renaming a component set changes the hash."""
        cs_before = [_make_component_set("Button", "aa")]
        cs_after = [_make_component_set("ButtonV2", "aa")]
        assert _compute_hash(cs_before) != _compute_hash(cs_after)

    def test_does_not_change_for_thumbnail_update(self):
        """INVARIANT: hash only covers (name, key) — content changes like updated_at are ignored."""
        cs_before = [{"name": "Button", "key": "aa", "updated_at": "2026-01-01T00:00:00Z"}]
        cs_after = [{"name": "Button", "key": "aa", "updated_at": "2026-06-15T12:00:00Z"}]
        assert _compute_hash(cs_before) == _compute_hash(cs_after)

    def test_returns_16_char_hex_string(self):
        """INVARIANT: hash is always a 16-character hexadecimal string."""
        h = _compute_hash([_make_component_set("Button", "aa")])
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


class TestHashRoundTrip:
    def test_existing_hash_recovers_hash_from_render_output(self, tmp_path: Path):
        """INVARIANT: _existing_hash can extract the content_hash written by _render.

        This is the most critical census test. If this round-trip breaks:
          _existing_hash returns None → None != content_hash → census writes every run
          → spurious git commits on every CI run, wasting enrichment budget.

        Breaking scenarios: renaming the frontmatter field, changing the render format,
        changing how _existing_hash parses the file.
        """
        cs = [_make_component_set("Button", "aabb1122")]
        content_hash = _compute_hash(cs)
        rendered = _render("key1", "Web App", cs, content_hash, "2026-01-01T00:00:00Z")

        path = tmp_path / "_census.md"
        path.write_text(rendered, encoding="utf-8")

        assert _existing_hash(path) == content_hash, (
            "Round-trip broken: _existing_hash cannot read back the hash written by _render. "
            "This means the skip check will always fire and census will write on every run."
        )

    def test_existing_hash_returns_none_for_missing_file(self, tmp_path: Path):
        """INVARIANT: _existing_hash returns None (not an error) when file does not exist."""
        assert _existing_hash(tmp_path / "nonexistent.md") is None

    def test_existing_hash_returns_none_for_corrupt_file(self, tmp_path: Path):
        """INVARIANT: _existing_hash returns None when the file has no parseable hash."""
        path = tmp_path / "_census.md"
        path.write_text("no frontmatter here\n", encoding="utf-8")
        assert _existing_hash(path) is None


class TestCensusSkipBehavior:
    async def _run_census(
        self,
        tmp_path: Path,
        component_sets: list[dict],
        force: bool = False,
    ) -> None:
        # Save state to disk so _run can load it (it creates its own FigmaSyncState)
        state = FigmaSyncState(tmp_path)
        state.load()
        state.add_tracked_file("key1", "Web App")
        state.save()

        mock_client = AsyncMock()
        mock_client.get_component_sets = AsyncMock(return_value=component_sets)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_class = MagicMock(return_value=mock_client)

        with patch.object(census_module, "FigmaClient", mock_client_class):
            await _run("fake-api-key", tmp_path, None, auto_commit=False, force=force)

    @pytest.mark.asyncio
    async def test_census_skips_write_when_hash_unchanged(self, tmp_path: Path):
        """INVARIANT: census does not rewrite the file when the component registry is unchanged.

        Same mechanism that prevents sidecar/catalog spurious writes — just implemented
        as a content_hash comparison instead of write_json_if_changed.
        """
        cs = [_make_component_set("Button", "aabb1122")]
        await self._run_census(tmp_path, cs)

        out = tmp_path / "figma" / file_slug_for_key("Web App", "key1") / "_census.md"
        assert out.exists()
        mtime_first = out.stat().st_mtime_ns

        await self._run_census(tmp_path, cs)

        assert out.stat().st_mtime_ns == mtime_first, (
            "Census rewrote the file despite unchanged component registry. "
            "This creates spurious git commits on every CI run."
        )

    @pytest.mark.asyncio
    async def test_census_writes_when_registry_changes(self, tmp_path: Path):
        """INVARIANT: census writes when a component is added to the registry."""
        cs_before = [_make_component_set("Button", "aabb1122")]
        await self._run_census(tmp_path, cs_before)

        out = tmp_path / "figma" / file_slug_for_key("Web App", "key1") / "_census.md"
        content_before = out.read_text()

        cs_after = [
            _make_component_set("Button", "aabb1122"),
            _make_component_set("Input", "ccdd3344"),
        ]
        await self._run_census(tmp_path, cs_after)

        assert out.read_text() != content_before

    @pytest.mark.asyncio
    async def test_census_rewrites_when_source_lifecycle_changes(self, tmp_path: Path):
        """INVARIANT TC-12: source provenance changes are meaningful registry metadata."""
        cs = [_make_component_set("Button", "aabb1122")]
        await self._run_census(tmp_path, cs)

        state = FigmaSyncState(tmp_path)
        state.load()
        state.manifest.files["key1"].source_project_id = "proj-archive"
        state.manifest.files["key1"].source_project_name = "ARCHIVE"
        state.manifest.files["key1"].source_lifecycle = "archived"
        state.save()
        await self._run_census(tmp_path, cs)

        out = tmp_path / "figma" / file_slug_for_key("Web App", "key1") / "_census.md"
        text = out.read_text()
        assert "source_project_id: proj-archive" in text
        assert "source_lifecycle: archived" in text

    @pytest.mark.asyncio
    async def test_census_reports_empty_registry_for_explicit_file_key(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        """INVARIANT REG-1: explicit census probes persist empty registry state.

        Silent no-op is fine for whole-repo census because most product files
        are not libraries. For ``--file-key`` diagnostics, the repo artifact
        must distinguish "probed and empty" from "not probed".
        """
        state = FigmaSyncState(tmp_path)
        state.load()
        state.add_tracked_file("key1", "Tap In Design System")
        state.save()

        mock_client = AsyncMock()
        mock_client.get_component_sets = AsyncMock(return_value=[])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_class = MagicMock(return_value=mock_client)

        with patch.object(census_module, "FigmaClient", mock_client_class):
            await _run("fake-api-key", tmp_path, "key1", auto_commit=False, force=False)

        assert "Tap In Design System: 0 published component set(s)" in capsys.readouterr().out
        out = tmp_path / "figma" / file_slug_for_key("Tap In Design System", "key1") / "_census.md"
        assert out.exists()
        text = out.read_text()
        assert "component_set_count: 0" in text
        assert "content_hash: 4f53cda18c2baa0c" in text
        assert "# Tap In Design System — Published Component Sets" in text

    @pytest.mark.asyncio
    async def test_census_frontmatter_records_source_lifecycle(self, tmp_path: Path):
        """INVARIANT TC-12: component census preserves source-system lifecycle."""
        state = FigmaSyncState(tmp_path)
        state.load()
        state.add_tracked_file("key1", "Legacy Components")
        state.manifest.files["key1"].source_project_id = "proj-archive"
        state.manifest.files["key1"].source_project_name = "ARCHIVE"
        state.manifest.files["key1"].source_lifecycle = "archived"
        state.save()

        cs = [_make_component_set("Button", "aabb1122")]
        mock_client = AsyncMock()
        mock_client.get_component_sets = AsyncMock(return_value=cs)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_class = MagicMock(return_value=mock_client)

        with patch.object(census_module, "FigmaClient", mock_client_class):
            await _run("fake-api-key", tmp_path, None, auto_commit=False, force=False)

        out = tmp_path / "figma" / file_slug_for_key("Legacy Components", "key1") / "_census.md"
        text = out.read_text()
        assert "source_project_id: proj-archive" in text
        assert "source_project_name: ARCHIVE" in text
        assert "source_lifecycle: archived" in text

    @pytest.mark.asyncio
    async def test_census_uses_latest_file_name_slug_from_manifest(self, tmp_path: Path):
        """INVARIANT: census path follows latest manifest file_name for this file key."""
        state = FigmaSyncState(tmp_path)
        state.load()
        state.add_tracked_file("key1", "Old Name")
        state.manifest.files["key1"].file_name = "New Name"
        state.save()

        cs = [_make_component_set("Button", "aabb1122")]
        mock_client = AsyncMock()
        mock_client.get_component_sets = AsyncMock(return_value=cs)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_class = MagicMock(return_value=mock_client)

        with patch.object(census_module, "FigmaClient", mock_client_class):
            await _run("fake-api-key", tmp_path, None, auto_commit=False, force=False)

        assert not (
            tmp_path / "figma" / file_slug_for_key("Old Name", "key1") / "_census.md"
        ).exists()
        assert (tmp_path / "figma" / file_slug_for_key("New Name", "key1") / "_census.md").exists()
