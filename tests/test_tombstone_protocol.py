"""Tests for the unresolvable-frame tombstone protocol (figmaclaw#121).

Protocol: when the same-run NO-PROGRESS guard fires, the dispatcher
records the current manifest frame_hash into
``frontmatter.unresolvable_frames[node_id]``. On the next run:

- ``pending_frame_node_ids`` / ``pending_sections`` exclude the
  tombstoned frame (terminal state honored) while the manifest hash
  still matches.
- ``enrichment_info`` no longer reports the file as needing enrichment
  solely because of the tombstoned row.
- When Figma content changes and the manifest hash diverges from the
  recorded tombstone, the tombstone auto-invalidates — the frame is
  pending again (one retry per content change).

Cross-cuts:
- The frame-keyed key-set invariant (figmaclaw#121, chokepoint prune)
  applies equally to ``unresolvable_frames``: orphan tombstones (keys
  not in ``frames``) are dropped on write.
- Tombstones are preserved across pulls by ``update_page_frontmatter``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from figmaclaw.commands.claude_run import (
    _record_tombstones,
    enrichment_info,
    pending_frame_node_ids,
    pending_sections,
)
from figmaclaw.figma_frontmatter import FigmaPageFrontmatter, FrameComposition
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import build_page_frontmatter
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.staleness import active_tombstoned_node_ids


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_manifest(repo_dir: Path, *, frame_hashes: dict[str, str]) -> None:
    """Write a minimal manifest so active_tombstoned_node_ids can read it."""
    state = FigmaSyncState(repo_dir)
    state.load()
    # Build manifest entries programmatically via the pydantic models.
    from figmaclaw.figma_sync_state import FileEntry, PageEntry

    page_entry = PageEntry(
        page_name="Page",
        page_slug="page",
        md_path="figma/fk/pages/page.md",
        page_hash="page-hash",
        frame_hashes=frame_hashes,
        last_refreshed_at="2026-04-18T00:00:00Z",
    )
    file_entry = FileEntry(
        file_name="Test",
        version="v1",
        last_modified="2026-04-18T00:00:00Z",
        last_refreshed_at="2026-04-18T00:00:00Z",
        pages={"1:1": page_entry},
    )
    state.manifest.files["fk"] = file_entry
    state.save()


def _write_page(
    md_path: Path,
    *,
    frames: list[str],
    unresolvable_frames: dict[str, str] | None = None,
    body_unresolved_frames: list[str] | None = None,
) -> None:
    """Write a minimal figmaclaw page .md with given frontmatter + body."""
    lines = [
        "---",
        "file_key: fk",
        "page_node_id: '1:1'",
        f"frames: {frames!r}",
        "enriched_schema_version: 1",
    ]
    if unresolvable_frames:
        lines.append(f"unresolvable_frames: {unresolvable_frames!r}")
    lines.append("---")
    lines.append("")
    lines.append("# Page")
    lines.append("")
    lines.append("## Section (`10:1`)")
    lines.append("")
    lines.append("| Screen | Node ID | Description |")
    lines.append("|--------|---------|-------------|")
    for nid in frames:
        if body_unresolved_frames and nid in body_unresolved_frames:
            lines.append(f"| Row | `{nid}` | (no screenshot available) |")
        else:
            lines.append(f"| Row | `{nid}` | A real description. |")
    md_path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# active_tombstoned_node_ids — hash-matching semantics
# ---------------------------------------------------------------------------


class TestActiveTombstonedNodeIds:
    def test_returns_empty_when_no_tombstones(self, tmp_path: Path) -> None:
        fm = FigmaPageFrontmatter(
            file_key="fk",
            page_node_id="1:1",
            frames=["11:1"],
        )
        assert active_tombstoned_node_ids(fm, tmp_path) == set()

    def test_returns_empty_when_repo_dir_is_none(self) -> None:
        fm = FigmaPageFrontmatter(
            file_key="fk",
            page_node_id="1:1",
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1"},
        )
        assert active_tombstoned_node_ids(fm, None) == set()

    def test_tombstone_active_when_hash_matches_manifest(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1", "11:2": "h2"})
        fm = FigmaPageFrontmatter(
            file_key="fk",
            page_node_id="1:1",
            frames=["11:1", "11:2"],
            unresolvable_frames={"11:1": "h1"},
        )
        assert active_tombstoned_node_ids(fm, tmp_path) == {"11:1"}

    def test_tombstone_invalid_when_hash_drifts(self, tmp_path: Path) -> None:
        """Simulate Figma content change: manifest now carries a different
        hash than the recorded tombstone. Tombstone is no longer active —
        the frame becomes pending again for one retry.
        """
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1-NEW"})
        fm = FigmaPageFrontmatter(
            file_key="fk",
            page_node_id="1:1",
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1-OLD"},
        )
        assert active_tombstoned_node_ids(fm, tmp_path) == set()

    def test_missing_manifest_entry_returns_empty(self, tmp_path: Path) -> None:
        """When the manifest has no entry for the file (never pulled), no
        tombstone is active — safe default is "retry" rather than "skip"."""
        _write_manifest(tmp_path, frame_hashes={})  # empty manifest pages
        fm = FigmaPageFrontmatter(
            file_key="other-key",  # not in manifest
            page_node_id="1:1",
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1"},
        )
        assert active_tombstoned_node_ids(fm, tmp_path) == set()


# ---------------------------------------------------------------------------
# pending_frame_node_ids / pending_sections — tombstone filtering
# ---------------------------------------------------------------------------


class TestPendingFiltersTombstones:
    def test_tombstoned_frame_is_not_pending_when_hash_matches(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1", "11:2": "h2"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1", "11:2"],
            unresolvable_frames={"11:1": "h1"},
            body_unresolved_frames=["11:1", "11:2"],
        )

        pending = pending_frame_node_ids(md, repo_dir=tmp_path)
        assert pending == {"11:2"}  # 11:1 filtered out by active tombstone

    def test_tombstoned_frame_is_pending_when_hash_drifted(self, tmp_path: Path) -> None:
        """Content changed after tombstone → tombstone no longer active →
        frame pending again. This is the retry-on-change contract.
        """
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1-NEW"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1-OLD"},
            body_unresolved_frames=["11:1"],
        )

        pending = pending_frame_node_ids(md, repo_dir=tmp_path)
        assert pending == {"11:1"}

    def test_pending_frame_node_ids_without_repo_dir_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        """Legacy callers without repo_dir see the pre-tombstone behavior.

        Important for callers that don't have manifest access — they get
        the raw unresolved set, same as before figmaclaw#121.
        """
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1"},
            body_unresolved_frames=["11:1"],
        )

        pending = pending_frame_node_ids(md)
        assert pending == {"11:1"}  # no filtering without repo_dir

    def test_pending_sections_drops_fully_tombstoned_sections(
        self, tmp_path: Path
    ) -> None:
        """When every pending frame in a section is tombstoned+matching,
        the section is fully "done" and drops out of pending_sections.
        """
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1", "11:2": "h2"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1", "11:2"],
            unresolvable_frames={"11:1": "h1", "11:2": "h2"},
            body_unresolved_frames=["11:1", "11:2"],
        )

        sections = pending_sections(md, repo_dir=tmp_path)
        assert sections == []


# ---------------------------------------------------------------------------
# enrichment_info — selector honors tombstones
# ---------------------------------------------------------------------------


class TestEnrichmentInfoHonorsTombstones:
    def test_all_tombstoned_file_is_not_enrichment_candidate(
        self, tmp_path: Path
    ) -> None:
        """The critical cross-run loop guard: after tombstones land for every
        unresolved row, the selector stops picking the file. This is what
        stops the hourly RED loop in figmaclaw#121.
        """
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1"},
            body_unresolved_frames=["11:1"],
        )
        # Anchor the schema to the current version so the "must update"
        # branch doesn't spuriously requeue the file.
        text = md.read_text()
        text = text.replace(
            "enriched_schema_version: 1",
            "enriched_schema_version: 1\nenriched_hash: anything",
        )
        md.write_text(text)

        needs_it, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_it is False

    def test_hash_drift_makes_file_enrichable_again(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1-NEW"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1-OLD"},
            body_unresolved_frames=["11:1"],
        )

        needs_it, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_it is True


# ---------------------------------------------------------------------------
# _record_tombstones — write-path behavior
# ---------------------------------------------------------------------------


class TestRecordTombstones:
    def test_writes_tombstones_with_current_manifest_hashes(
        self, tmp_path: Path
    ) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1", "11:2": "h2"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1", "11:2"],
            body_unresolved_frames=["11:1", "11:2"],
        )

        added = _record_tombstones(md, tmp_path, {"11:1", "11:2"})
        assert added == 2

        fm = parse_frontmatter(md.read_text())
        assert fm is not None
        assert fm.unresolvable_frames == {"11:1": "h1", "11:2": "h2"}

    def test_skips_node_ids_missing_from_manifest(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1"})  # no "11:2"
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1", "11:2"],
            body_unresolved_frames=["11:1", "11:2"],
        )

        added = _record_tombstones(md, tmp_path, {"11:1", "11:2"})
        assert added == 1
        fm = parse_frontmatter(md.read_text())
        assert fm.unresolvable_frames == {"11:1": "h1"}

    def test_does_not_duplicate_existing_tombstones(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1"},
            body_unresolved_frames=["11:1"],
        )

        added = _record_tombstones(md, tmp_path, {"11:1"})
        assert added == 0

    def test_preserves_body_verbatim(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1"],
            body_unresolved_frames=["11:1"],
        )
        body_before = md.read_text().split("---\n", 2)[2]

        _record_tombstones(md, tmp_path, {"11:1"})

        body_after = md.read_text().split("---\n", 2)[2]
        assert body_after == body_before


# ---------------------------------------------------------------------------
# Cross-cutting: key-set invariant applies to unresolvable_frames
# ---------------------------------------------------------------------------


class TestKeySetInvariantAppliesToTombstones:
    def test_orphan_tombstones_pruned_on_write(self) -> None:
        """A tombstone for a node_id no longer in frames gets dropped by
        the frontmatter-write chokepoint. Same invariant as every other
        frame-keyed dict.
        """
        page = FigmaPage(
            file_key="fk",
            file_name="F",
            page_node_id="1:1",
            page_name="Page",
            page_slug="page",
            figma_url="https://figma.com/design/fk?node-id=1-1",
            sections=[
                FigmaSection(
                    node_id="10:10",
                    name="Section",
                    frames=[FigmaFrame(node_id="11:1", name="F1")],
                )
            ],
            flows=[],
            version="v",
            last_modified="2026-04-18T00:00:00Z",
        )
        fm_text = build_page_frontmatter(
            page,
            unresolvable_frames={"11:1": "h1", "DEAD:1": "h-dead"},
        )

        parsed = parse_frontmatter(f"{fm_text}\n\n# body\n")
        assert parsed is not None
        assert parsed.unresolvable_frames == {"11:1": "h1"}
        assert "DEAD:1" not in parsed.unresolvable_frames


# ---------------------------------------------------------------------------
# Cross-run: run N writes tombstone, run N+1 skips the file
# ---------------------------------------------------------------------------


class TestCrossRunIdempotency:
    def test_second_run_does_not_reselect_fully_tombstoned_file(
        self, tmp_path: Path
    ) -> None:
        """End-to-end cross-run shape (figmaclaw#121):

            state_0: file has an unresolved body row with no screenshot
            run 1:   NO-PROGRESS fires → _record_tombstones writes
                     unresolvable_frames[node_id] = manifest_hash
            state_1: file has both the body row AND the tombstone
            run 2:   enrichment_info returns needs_it=False, because the
                     tombstone matches the current manifest hash.

        This is the only bug shape row 9 YELLOW + tombstones together
        are meant to close. Without tombstones, run 2 would keep
        selecting the file forever.
        """
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1"})
        md = tmp_path / "page.md"
        _write_page(
            md,
            frames=["11:1"],
            body_unresolved_frames=["11:1"],
        )
        # Anchor schema so "must update" doesn't override
        text = md.read_text()
        text = text.replace(
            "enriched_schema_version: 1",
            "enriched_schema_version: 1\nenriched_hash: anything",
        )
        md.write_text(text)

        # Run 1: selector says yes (body has unresolved row, no tombstone yet)
        needs_run1, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_run1 is True

        # Simulate NO-PROGRESS: LLM wrote "(no screenshot available)" and
        # dispatcher recorded tombstones
        added = _record_tombstones(md, tmp_path, {"11:1"})
        assert added == 1

        # Run 2: selector now says no — tombstone matches manifest hash
        needs_run2, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_run2 is False
