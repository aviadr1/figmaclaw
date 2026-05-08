"""Tests for figma_paths.py, figma_hash.py, and figma_sync_state.py."""

from __future__ import annotations

import json
from pathlib import Path

from figmaclaw.figma_hash import compute_page_hash
from figmaclaw.figma_paths import file_slug_for_key, page_path, slugify
from figmaclaw.figma_sync_state import FigmaSyncState, Manifest

# --- figma_paths ---


def test_page_path_format():
    """INVARIANT: page_path returns figma/{file_slug}/pages/{slug}.md"""
    result = page_path("web-app", "onboarding")
    assert result == "figma/web-app/pages/onboarding.md"


def test_slugify_lowercases():
    assert slugify("Onboarding") == "onboarding"


def test_slugify_replaces_spaces_with_hyphens():
    assert slugify("schedule event") == "schedule-event"


def test_slugify_strips_special_chars():
    assert slugify("reach - auto content sharing") == "reach-auto-content-sharing"


def test_slugify_collapses_multiple_hyphens():
    assert slugify("home--feed") == "home-feed"


def test_slugify_strips_leading_trailing_hyphens():
    assert slugify("  --home--  ") == "home"


def test_slugify_handles_unicode():
    result = slugify("🎨 Designs")
    assert result  # non-empty
    assert "-" not in result[:1]  # no leading hyphen


def test_file_slug_for_key_always_appends_full_file_key_when_unique():
    tracked = {
        "abc123": "Web App",
        "def456": "Design System",
    }
    assert file_slug_for_key("Web App", "abc123", tracked_file_names=tracked) == "web-app-abc123"


def test_file_slug_for_key_uses_full_key_when_slug_collides():
    tracked = {
        "hOV4QMBnDIG5s5OYkSrX9E": "Web App",
        "jb1bZRQUUOQKEpb5p6vt5e": "Web App",
    }
    assert (
        file_slug_for_key("Web App", "hOV4QMBnDIG5s5OYkSrX9E", tracked_file_names=tracked)
        == "web-app-hOV4QMBnDIG5s5OYkSrX9E"
    )


# --- figma_hash ---


def _page_node(sections: list[dict]) -> dict:
    return {"id": "0:1", "name": "Page", "type": "CANVAS", "children": sections}


def _section(sid: str, name: str, frames: list[dict]) -> dict:
    return {"id": sid, "name": name, "type": "SECTION", "children": frames}


def _frame(fid: str, name: str) -> dict:
    return {"id": fid, "name": name, "type": "FRAME", "children": []}


def test_hash_is_stable_for_identical_input():
    """INVARIANT: Same structure always produces same hash."""
    page = _page_node([_section("10:1", "auth", [_frame("11:1", "login")])])
    h1 = compute_page_hash(page)
    h2 = compute_page_hash(page)
    assert h1 == h2


def test_hash_changes_when_frame_name_changes():
    """INVARIANT: Renaming a frame changes the hash."""
    page1 = _page_node([_section("10:1", "auth", [_frame("11:1", "login")])])
    page2 = _page_node([_section("10:1", "auth", [_frame("11:1", "sign in")])])
    assert compute_page_hash(page1) != compute_page_hash(page2)


def test_hash_changes_when_frame_added():
    """INVARIANT: Adding a frame changes the hash."""
    page1 = _page_node([_section("10:1", "auth", [_frame("11:1", "login")])])
    page2 = _page_node(
        [_section("10:1", "auth", [_frame("11:1", "login"), _frame("11:2", "register")])]
    )
    assert compute_page_hash(page1) != compute_page_hash(page2)


def test_hash_is_order_independent():
    """INVARIANT: Hash is stable regardless of JSON child ordering (canonical sort)."""
    page1 = _page_node(
        [_section("10:1", "auth", [_frame("11:1", "login"), _frame("11:2", "register")])]
    )
    # Same content, but build the internal list in reverse order
    node = {
        "id": "0:1",
        "name": "Page",
        "type": "CANVAS",
        "children": [
            {
                "id": "10:1",
                "name": "auth",
                "type": "SECTION",
                "children": [
                    _frame("11:2", "register"),
                    _frame("11:1", "login"),
                ],
            }
        ],
    }
    # Both pages have same IDs/names — hash must match
    assert compute_page_hash(page1) == compute_page_hash(node)


def test_hash_not_affected_by_extra_visual_fields():
    """INVARIANT: Visual-only fields (position, fills) don't affect the hash."""
    base_frame = {"id": "11:1", "name": "login", "type": "FRAME", "children": []}
    styled_frame = {
        **base_frame,
        "absoluteBoundingBox": {"x": 100, "y": 200, "width": 375, "height": 812},
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
    }
    page1 = _page_node([_section("10:1", "auth", [base_frame])])
    page2 = _page_node([_section("10:1", "auth", [styled_frame])])
    assert compute_page_hash(page1) == compute_page_hash(page2)


def test_hash_returns_16_char_string():
    """INVARIANT: Hash is a fixed-length hex string."""
    page = _page_node([])
    h = compute_page_hash(page)
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


# --- figma_sync_state ---


def test_manifest_is_pydantic_model():
    """INVARIANT: Manifest is a Pydantic BaseModel."""
    import pydantic

    assert issubclass(Manifest, pydantic.BaseModel)


def test_sync_state_load_noop_when_missing(tmp_path: Path):
    """INVARIANT: load() is a no-op when manifest file doesn't exist."""
    state = FigmaSyncState(tmp_path)
    state.load()  # must not raise
    assert state.manifest.tracked_files == []
    assert state.manifest.files == {}


def test_sync_state_save_then_load_round_trips(tmp_path: Path):
    """INVARIANT: save() followed by load() restores exact state."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.tracked_files.append("abc123")
    from figmaclaw.figma_sync_state import FileEntry, PageEntry

    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"] = FileEntry(
        file_name="Web App",
        version="999",
        last_modified="2026-03-31T00:00:00Z",
        last_checked_at="2026-03-31T01:00:00Z",
        pages={
            "0:1": PageEntry(
                page_name="Onboarding",
                page_slug="onboarding",
                md_path="figma/abc123/pages/onboarding.md",
                page_hash="deadbeef12345678",
                last_refreshed_at="2026-03-31T01:00:00Z",
            )
        },
    )
    state.save()

    state2 = FigmaSyncState(tmp_path)
    state2.load()
    assert "abc123" in state2.manifest.tracked_files
    assert state2.manifest.files["abc123"].file_name == "Web App"
    assert state2.manifest.files["abc123"].pages["0:1"].page_hash == "deadbeef12345678"


def test_manifest_v1_load_migrates_file_schema_to_page_schema(tmp_path: Path):
    """INVARIANT: legacy manifests preserve schema state when loaded as v2."""
    import json

    manifest_path = tmp_path / ".figma-sync" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tracked_files": ["abc123"],
                "files": {
                    "abc123": {
                        "file_name": "Web App",
                        "version": "v1",
                        "last_modified": "2026-03-31T00:00:00Z",
                        "pull_schema_version": 7,
                        "pages": {
                            "0:1": {
                                "page_name": "Onboarding",
                                "page_slug": "onboarding-0-1",
                                "md_path": "figma/web-app-abc123/pages/onboarding-0-1.md",
                                "page_hash": "deadbeef12345678",
                                "last_refreshed_at": "2026-03-31T01:00:00Z",
                                "component_md_paths": [
                                    "figma/web-app-abc123/components/buttons-2-1.md"
                                ],
                                "frame_hashes": {"1:1": "aaaabbbb"},
                            }
                        },
                    }
                },
            }
        )
    )

    state = FigmaSyncState(tmp_path)
    state.load()
    page = state.manifest.files["abc123"].pages["0:1"]

    assert state.manifest.schema_version == 2
    assert page.pull_schema_version == 7
    assert page.component_schema_versions == {"figma/web-app-abc123/components/buttons-2-1.md": 7}


def test_manifest_preserves_unknown_future_schema_fields(tmp_path: Path):
    """INVARIANT: old writers must not erase manifest fields added by newer schemas."""
    manifest_path = tmp_path / ".figma-sync" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "future_root_field": {"kept": True},
                "tracked_files": ["abc123"],
                "files": {
                    "abc123": {
                        "file_name": "Web App",
                        "version": "v1",
                        "last_modified": "2026-03-31T00:00:00Z",
                        "pull_schema_version": 7,
                        "future_file_field": "do-not-drop",
                        "pages": {
                            "0:1": {
                                "page_name": "Onboarding",
                                "page_slug": "onboarding-0-1",
                                "md_path": "figma/web-app-abc123/pages/onboarding-0-1.md",
                                "page_hash": "deadbeef12345678",
                                "last_refreshed_at": "2026-03-31T01:00:00Z",
                                "future_page_field": [1, 2, 3],
                            }
                        },
                    }
                },
            }
        )
    )

    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.files["abc123"].last_modified = "2026-04-01T00:00:00Z"
    state.save()

    payload = json.loads(manifest_path.read_text())
    assert payload["schema_version"] == 99
    assert payload["future_root_field"] == {"kept": True}
    assert payload["files"]["abc123"]["future_file_field"] == "do-not-drop"
    assert payload["files"]["abc123"]["pages"]["0:1"]["future_page_field"] == [1, 2, 3]


def test_sync_state_save_skips_manifest_timestamp_only_changes(tmp_path: Path):
    """W-1: manifest timestamp-only updates must not rewrite the committed cache.

    linear-git exposed commits where only ``last_checked_at`` /
    ``last_refreshed_at`` changed. Those commits wake downstream CI and can
    trigger avoidable enrichment work despite carrying no load-bearing state.
    """
    from figmaclaw.figma_sync_state import FileEntry, PageEntry

    state = FigmaSyncState(tmp_path)
    state.manifest.tracked_files.append("abc123")
    state.manifest.files["abc123"] = FileEntry(
        file_name="Web App",
        version="v1",
        last_modified="2026-04-28T00:00:00Z",
        last_checked_at="2026-05-05T00:00:00Z",
        pages={
            "0:1": PageEntry(
                page_name="Onboarding",
                page_slug="onboarding",
                md_path="figma/web-app-abc123/pages/onboarding.md",
                page_hash="deadbeef12345678",
                last_refreshed_at="2026-05-05T00:00:00Z",
                frame_hashes={"1:1": "aaaabbbb"},
            )
        },
    )
    state.save()
    manifest_path = tmp_path / ".figma-sync" / "manifest.json"
    original = manifest_path.read_text()

    state.manifest.files["abc123"].last_checked_at = "2026-05-05T01:00:00Z"
    state.manifest.files["abc123"].pages["0:1"].last_refreshed_at = "2026-05-05T01:00:00Z"
    state.save()

    assert manifest_path.read_text() == original

    state.manifest.files["abc123"].version = "v2"
    state.save()

    updated = manifest_path.read_text()
    assert updated != original
    assert '"version": "v2"' in updated
    assert "2026-05-05T01:00:00Z" in updated


def test_sync_state_get_page_hash_returns_none_for_unknown(tmp_path: Path):
    """INVARIANT: get_page_hash returns None for a page not yet in manifest."""
    state = FigmaSyncState(tmp_path)
    state.load()
    assert state.get_page_hash("abc123", "0:1") is None


def test_sync_state_add_tracked_file_prevents_duplicates(tmp_path: Path):
    """INVARIANT: Adding the same file twice does not create duplicate entries."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.add_tracked_file("abc123", "Web App")
    assert state.manifest.tracked_files.count("abc123") == 1


def test_sync_state_manifest_written_as_json(tmp_path: Path):
    """INVARIANT: Manifest is persisted as valid JSON."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.save()

    manifest_file = tmp_path / ".figma-sync" / "manifest.json"
    assert manifest_file.exists()
    data = json.loads(manifest_file.read_text())
    assert data["tracked_files"] == ["abc123"]


# --- should_skip_page ---


def test_should_skip_page_matches_old_prefix(tmp_path: Path):
    """INVARIANT: Pages named 'old-*' are skipped by default."""
    state = FigmaSyncState(tmp_path)
    state.load()
    assert state.should_skip_page("old-components") is True
    assert state.should_skip_page("old-concept-community") is True


def test_should_skip_page_matches_old_space_prefix(tmp_path: Path):
    """INVARIANT: Pages named 'old *' (with space) are skipped by default."""
    state = FigmaSyncState(tmp_path)
    state.load()
    assert state.should_skip_page("old concept") is True
    assert state.should_skip_page("old gigaverse design system") is True


def test_should_skip_page_matches_separator(tmp_path: Path):
    """INVARIANT: Pages named '---' (separator) are skipped by default."""
    state = FigmaSyncState(tmp_path)
    state.load()
    assert state.should_skip_page("---") is True


def test_should_skip_page_is_case_insensitive(tmp_path: Path):
    """INVARIANT: skip_pages matching is case-insensitive."""
    state = FigmaSyncState(tmp_path)
    state.load()
    assert state.should_skip_page("OLD-Components") is True
    assert state.should_skip_page("OLD CONCEPT") is True


def test_should_skip_page_does_not_skip_normal_page(tmp_path: Path):
    """INVARIANT: Normal pages are not skipped."""
    state = FigmaSyncState(tmp_path)
    state.load()
    assert state.should_skip_page("Onboarding") is False
    assert state.should_skip_page("Home Feed") is False
    assert state.should_skip_page("Components") is False


def test_should_skip_page_respects_custom_patterns(tmp_path: Path):
    """INVARIANT: Custom skip_pages patterns in the manifest are respected."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.skip_pages = ["archive*", "📦*"]
    assert state.should_skip_page("archive-v1") is True
    assert state.should_skip_page("📦 Icons") is True
    assert state.should_skip_page("Onboarding") is False


def test_skip_pages_default_values_persisted_to_manifest(tmp_path: Path):
    """INVARIANT: Default skip_pages patterns are written to manifest.json on save."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.save()

    data = json.loads((tmp_path / ".figma-sync" / "manifest.json").read_text())
    assert "old-*" in data["skip_pages"]
    assert "old *" in data["skip_pages"]
    assert "---" in data["skip_pages"]


def test_skip_pages_custom_patterns_round_trip(tmp_path: Path):
    """INVARIANT: Custom skip_pages patterns survive a save/load round-trip."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.skip_pages = ["archive*", "wip-*"]
    state.save()

    state2 = FigmaSyncState(tmp_path)
    state2.load()
    assert state2.manifest.skip_pages == ["archive*", "wip-*"]
