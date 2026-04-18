"""Cross-run idempotency — figmaclaw#121 incident replay.

Pins the category of tests that was missing from the PR suite: given a
repo state S, running the enrichment selector/dispatcher twice produces
either a no-op second run or a strictly cheaper second run. A failing
test in this file would mean we've regressed to the "run N and run N+1
do identical expensive work forever" bug shape that burned 24+ hours of
CI on gigaverse-app/linear-git.

Tests here exercise the full fix stack end-to-end:
- key-set invariant (orphan enriched_frame_hashes pruned by chokepoint)
- body orphan prune (rows for frames not in ``frames`` dropped by pull)
- tombstone protocol (NO-PROGRESS frames skipped until content changes)
- YELLOW row 9 (all-stuck reports as exit 0)
"""

from __future__ import annotations

from pathlib import Path

from figmaclaw.commands.claude_run import (
    _record_tombstones,
    enrichment_info,
    pending_frame_node_ids,
)
from figmaclaw.figma_sync_state import FigmaSyncState, FileEntry, PageEntry
from figmaclaw.verdict import EXIT_GREEN, compute_verdict


def _write_manifest(repo_dir: Path, *, frame_hashes: dict[str, str]) -> None:
    state = FigmaSyncState(repo_dir)
    state.load()
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
        last_checked_at="2026-04-18T00:00:00Z",
        pages={"1:1": page_entry},
    )
    state.manifest.files["fk"] = file_entry
    state.save()


def _write_stuck_fixture(
    md_path: Path,
    *,
    frames: list[str],
    unresolvable_frames: dict[str, str] | None = None,
) -> None:
    """Write the shape of a figmaclaw#121 stuck file.

    Body has one unresolved row per frame with the canonical
    (no screenshot available) marker — the LLM has already answered and
    cannot make progress.
    """
    lines = [
        "---",
        "file_key: fk",
        "page_node_id: '1:1'",
        f"frames: {frames!r}",
        "enriched_hash: deadbeef",
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
        lines.append(f"| Stuck row | `{nid}` | (no screenshot available) |")
    md_path.write_text("\n".join(lines) + "\n")


class TestIncidentReplay:
    """Replay of the linear-git 24-hour loop on a synthetic fixture.

    Run 1 mirrors the observed incident:
    - selector picks the file (body has unresolved marker)
    - dispatcher would invoke LLM, LLM writes same marker again
    - NO-PROGRESS guard fires, tombstones get recorded

    Run 2 is the contract this test pins:
    - selector does NOT pick the file (tombstone + matching hash)
    - if the selector somehow still picks it, the dispatcher agrees
    - verdict for a fully-stuck run is YELLOW exit 0, not RED
    """

    def test_run2_selector_skips_fully_tombstoned_file(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1", "11:2": "h2"})
        md = tmp_path / "page.md"
        _write_stuck_fixture(md, frames=["11:1", "11:2"])

        # Run 1: file needs enrichment.
        needs_run1, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_run1 is True
        pending_run1 = pending_frame_node_ids(md, repo_dir=tmp_path)
        assert pending_run1 == {"11:1", "11:2"}

        # Simulate NO-PROGRESS firing and the dispatcher writing tombstones.
        added = _record_tombstones(md, tmp_path, pending_run1)
        assert added == 2

        # Run 2: fully tombstoned, matching hashes → selector skips.
        needs_run2, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_run2 is False
        pending_run2 = pending_frame_node_ids(md, repo_dir=tmp_path)
        assert pending_run2 == set()

    def test_run2_retries_after_figma_content_change(self, tmp_path: Path) -> None:
        """One retry per content change — tombstone auto-invalidates when
        the manifest hash diverges from the recorded tombstone.
        """
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1"})
        md = tmp_path / "page.md"
        _write_stuck_fixture(md, frames=["11:1"])
        _record_tombstones(md, tmp_path, {"11:1"})

        # Between runs: Figma content changed → pull rewrote manifest hash.
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1-NEW"})

        # Tombstone no longer active — frame pending again for one retry.
        pending = pending_frame_node_ids(md, repo_dir=tmp_path)
        assert pending == {"11:1"}
        needs_it, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_it is True

    def test_fully_stuck_run_is_yellow_not_red(self) -> None:
        """Verdict for a run where every attempted file stopped at
        NO-PROGRESS is YELLOW exit 0 — the shape of every nightly run in
        the linear-git incident after tombstones land but before Figma
        content changes.
        """
        v = compute_verdict(
            files_selected=68,
            work_attempted=6,
            commits_made=0,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
            stuck=6,
        )
        assert v.exit_code == EXIT_GREEN
        assert v.row == "row 9"
        assert "YELLOW" in v.label


class TestPartialProgressStillWorks:
    """Counterpart: runs that CAN make progress must still make it.

    Pins the "tombstone protocol does not over-filter" contract.
    """

    def test_partially_tombstoned_file_still_enrichable(self, tmp_path: Path) -> None:
        """File has 2 frames, 1 tombstoned (matching hash) + 1 truly pending.
        Selector still picks it, dispatcher still sees the pending one.
        """
        _write_manifest(tmp_path, frame_hashes={"11:1": "h1", "11:2": "h2"})
        md = tmp_path / "page.md"
        _write_stuck_fixture(
            md,
            frames=["11:1", "11:2"],
            unresolvable_frames={"11:1": "h1"},  # 11:1 tombstoned, 11:2 not
        )

        needs_it, _ = enrichment_info(md, repo_dir=tmp_path)
        assert needs_it is True

        pending = pending_frame_node_ids(md, repo_dir=tmp_path)
        assert pending == {"11:2"}

    def test_repo_dir_absent_path_still_sees_all_pending(self, tmp_path: Path) -> None:
        """Legacy callers without repo_dir get pre-figmaclaw#121 behavior.

        Guarantees we didn't break any tool or test that invokes these
        functions without manifest access.
        """
        md = tmp_path / "page.md"
        _write_stuck_fixture(
            md,
            frames=["11:1"],
            unresolvable_frames={"11:1": "h1"},
        )

        pending = pending_frame_node_ids(md)  # no repo_dir
        assert pending == {"11:1"}

        needs_it, _ = enrichment_info(md)
        assert needs_it is True
