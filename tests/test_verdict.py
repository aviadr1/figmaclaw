"""Tests for figmaclaw.verdict (figmaclaw#27).

``compute_verdict`` is a pure function from six observable counters to a
(label, exit_code, row) tuple. Every row of the decision table has a test.
The row-5 phantom-selection test is the regression test for the
2026-04-05 create-community incident — it must never go GREEN again.

INVARIANT: row 5 (phantom selection) is evaluated FIRST and wins over
every other row. A phantom-selected file makes the run RED even if all
other files succeeded. Removing or weakening this precedence would hide
the exact class of bugs figmaclaw#27 was written to surface.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

from figmaclaw.verdict import (
    EXIT_GREEN,
    EXIT_RED,
    RunVerdict,
    compute_verdict,
    format_step_summary,
)


class TestSyntaxValidity:
    def test_verdict_compiles(self) -> None:
        script = Path(__file__).parent.parent / "figmaclaw" / "verdict.py"
        py_compile.compile(str(script), doraise=True)


# ---------------------------------------------------------------------------
# Decision table — one test per row.
# ---------------------------------------------------------------------------


class TestRow1NoOp:
    """Row 1: selector empty OR all files raced to enriched → GREEN (no-op)."""

    def test_empty_selector_is_green(self) -> None:
        v = compute_verdict(
            files_selected=0,
            work_attempted=0,
            commits_made=0,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_GREEN
        assert v.row == "row 1"
        assert "no-op" in v.label.lower()

    def test_all_files_raced_to_enriched_is_green(self) -> None:
        """Benign race: concurrent run enriched every selected file before
        this run could re-check them. Not phantom selection (no dispatcher
        disagreement), not a silent dispatch failure (never tried). Row 1.
        """
        v = compute_verdict(
            files_selected=5,
            work_attempted=0,
            commits_made=0,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_GREEN
        assert v.row == "row 1"


class TestRow2CleanCompletion:
    """Row 2: work attempted, commits landed, no errors → GREEN (clean)."""

    def test_clean_completion(self) -> None:
        v = compute_verdict(
            files_selected=5,
            work_attempted=5,
            commits_made=5,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_GREEN
        assert v.row == "row 2"
        assert "clean" in v.label.lower()


class TestRow3BudgetLimited:
    """Row 3: budget_exhausted=True with at least one commit → GREEN."""

    def test_budget_limited_partial_is_green(self) -> None:
        v = compute_verdict(
            files_selected=10,
            work_attempted=6,
            commits_made=6,
            errors=0,
            budget_exhausted=True,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_GREEN
        assert v.row == "row 3"
        assert "budget" in v.label.lower()

    def test_budget_limited_distinguishable_from_row_2(self) -> None:
        """Same counters + budget_exhausted flips the label (not the color)."""
        clean = compute_verdict(
            files_selected=10,
            work_attempted=6,
            commits_made=6,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        # Note: files_selected=10 but only 6 attempted/committed without
        # budget_exhausted would be unusual but the counters alone are legal.
        budget = compute_verdict(
            files_selected=10,
            work_attempted=6,
            commits_made=6,
            errors=0,
            budget_exhausted=True,
            skipped_no_work=0,
        )
        assert clean.label != budget.label
        assert clean.exit_code == budget.exit_code == EXIT_GREEN


class TestRow4ErrorRatioGate:
    """Row 4a (minority errors) → GREEN. Row 4b (majority) → RED."""

    def test_minority_errors_is_green(self) -> None:
        # 1 of 10 files errored, 9 committed → tolerable
        v = compute_verdict(
            files_selected=10,
            work_attempted=10,
            commits_made=9,
            errors=1,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_GREEN
        assert v.row == "row 4a"
        assert "minority" in v.label.lower()

    def test_exactly_half_errors_is_still_green(self) -> None:
        # 5 of 10 = 50% is NOT > 50%, falls through to row 4a.
        v = compute_verdict(
            files_selected=10,
            work_attempted=10,
            commits_made=5,
            errors=5,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_GREEN
        assert v.row == "row 4a"

    def test_majority_errors_is_red(self) -> None:
        # 6 of 10 errored = 60% > 50% → RED even though 4 commits landed
        v = compute_verdict(
            files_selected=10,
            work_attempted=10,
            commits_made=4,
            errors=6,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 4b"
        assert "majority" in v.label.lower()

    def test_majority_errors_beats_budget_exhausted(self) -> None:
        """Even if budget-exhausted, majority failure is still RED."""
        v = compute_verdict(
            files_selected=10,
            work_attempted=10,
            commits_made=3,
            errors=7,
            budget_exhausted=True,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 4b"


class TestRow5PhantomSelection:
    """Row 5: skipped_no_work > 0 → RED regardless of other counters.

    This is the regression test for the 2026-04-05 create-community
    incident. Row 5 must never be defeasible by success on other files,
    budget_exhausted, or anything else. It wins.
    """

    def test_single_phantom_file_is_red(self) -> None:
        v = compute_verdict(
            files_selected=1,
            work_attempted=0,
            commits_made=0,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=1,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 5"
        assert "phantom" in v.label.lower()

    def test_phantom_beats_row_2_success(self) -> None:
        """9 files succeeded, 1 phantom-selected — still RED.

        This is the exact invariant that makes row 5 useful. Without it,
        a single phantom-selected file would blend into the happy path
        every time another file succeeded in the same run.
        """
        v = compute_verdict(
            files_selected=10,
            work_attempted=9,
            commits_made=9,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=1,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 5"

    def test_phantom_beats_row_3_budget_limited(self) -> None:
        """Budget-exhausted with one phantom-selected file — still RED."""
        v = compute_verdict(
            files_selected=10,
            work_attempted=5,
            commits_made=5,
            errors=0,
            budget_exhausted=True,
            skipped_no_work=1,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 5"

    def test_phantom_beats_errors(self) -> None:
        """Phantom selection dominates even a mixed error state."""
        v = compute_verdict(
            files_selected=10,
            work_attempted=8,
            commits_made=6,
            errors=2,
            budget_exhausted=False,
            skipped_no_work=1,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 5"

    def test_multiple_phantom_files_still_row_5(self) -> None:
        v = compute_verdict(
            files_selected=5,
            work_attempted=3,
            commits_made=3,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=2,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 5"


class TestRow6SilentDispatchFailure:
    """Row 6: work attempted, zero commits, zero errors → RED (silent)."""

    def test_silent_dispatch_failure(self) -> None:
        v = compute_verdict(
            files_selected=3,
            work_attempted=3,
            commits_made=0,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 6"
        assert "silent" in v.label.lower()


class TestRow7ClassicFailure:
    """Row 7: work attempted, zero commits, errors present → RED."""

    def test_classic_failure(self) -> None:
        v = compute_verdict(
            files_selected=3,
            work_attempted=3,
            commits_made=0,
            errors=3,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert v.exit_code == EXIT_RED
        assert v.row == "row 7"


# ---------------------------------------------------------------------------
# Exit code contract — every RED must map to exit 2, every GREEN to 0.
# ---------------------------------------------------------------------------


class TestExitCodeContract:
    """No 'soft red' — every RED label pairs with EXIT_RED, every GREEN with EXIT_GREEN."""

    # (files_selected, work_attempted, commits_made, errors, budget_exhausted, skipped_no_work)
    SCENARIOS: list[tuple[int, int, int, int, bool, int]] = [
        (0, 0, 0, 0, False, 0),  # row 1
        (5, 5, 5, 0, False, 0),  # row 2
        (5, 3, 3, 0, True, 0),  # row 3
        (10, 10, 9, 1, False, 0),  # row 4a
        (10, 10, 3, 7, False, 0),  # row 4b
        (5, 4, 4, 0, False, 1),  # row 5
        (3, 3, 0, 0, False, 0),  # row 6
        (3, 3, 0, 3, False, 0),  # row 7
    ]

    def _verdict(
        self,
        scenario: tuple[int, int, int, int, bool, int],
    ) -> RunVerdict:
        fs, wa, cm, er, be, sn = scenario
        return compute_verdict(
            files_selected=fs,
            work_attempted=wa,
            commits_made=cm,
            errors=er,
            budget_exhausted=be,
            skipped_no_work=sn,
        )

    def test_label_color_matches_exit_code(self) -> None:
        for s in self.SCENARIOS:
            v = self._verdict(s)
            if v.label.startswith("GREEN"):
                assert v.exit_code == EXIT_GREEN, f"{s} -> {v}"
            elif v.label.startswith("RED"):
                assert v.exit_code == EXIT_RED, f"{s} -> {v}"
            else:
                raise AssertionError(
                    f"verdict label must start with GREEN or RED: {v.label}",
                )


# ---------------------------------------------------------------------------
# Step summary formatter — deterministic output for CI step summaries.
# ---------------------------------------------------------------------------


class TestFormatStepSummary:
    """Snapshot-style tests for the Markdown rendered to $GITHUB_STEP_SUMMARY."""

    def test_clean_completion_summary(self) -> None:
        v = RunVerdict(label="GREEN (clean completion)", exit_code=EXIT_GREEN, row="row 2")
        out = format_step_summary(
            verdict=v,
            files_selected=5,
            work_attempted=5,
            commits_made=5,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        assert "claude-run summary" in out
        assert "| files_selected | 5 |" in out
        assert "| commits_made | 5 |" in out
        assert "| budget_exhausted | false |" in out
        assert "**Verdict (row 2): GREEN (clean completion)**" in out
        # No phantom section when skipped_no_work=0
        assert "Phantom" not in out

    def test_phantom_selection_summary_includes_file_list(self) -> None:
        v = RunVerdict(label="RED (phantom selection)", exit_code=EXIT_RED, row="row 5")
        out = format_step_summary(
            verdict=v,
            files_selected=1,
            work_attempted=0,
            commits_made=0,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=1,
            phantom_files=["figma/community/pages/create-community-13957-217116.md"],
        )
        assert "**Verdict (row 5): RED (phantom selection)**" in out
        assert "Phantom-selected files" in out
        assert "create-community-13957-217116.md" in out

    def test_budget_stop_summary_includes_reason(self) -> None:
        v = RunVerdict(
            label="GREEN (budget-limited partial progress)",
            exit_code=EXIT_GREEN,
            row="row 3",
        )
        reason = (
            "[budget] elapsed=3006s remaining=174s ... "
            "→ STOP history=[6.4, 2.5, 2.9, 3.3, 5.9, 7.5, 6.5]"
        )
        out = format_step_summary(
            verdict=v,
            files_selected=8,
            work_attempted=7,
            commits_made=7,
            errors=0,
            budget_exhausted=True,
            skipped_no_work=0,
            budget_stop_reason=reason,
        )
        assert "budget-limited" in out
        assert "Budget stop" in out
        assert reason in out

    def test_summary_trailing_newline_safe(self) -> None:
        """Output should be safe to append to $GITHUB_STEP_SUMMARY multiple times."""
        v = RunVerdict(label="GREEN (no-op)", exit_code=EXIT_GREEN, row="row 1")
        out = format_step_summary(
            verdict=v,
            files_selected=0,
            work_attempted=0,
            commits_made=0,
            errors=0,
            budget_exhausted=False,
            skipped_no_work=0,
        )
        # The output is stable — no trailing whitespace on non-blank lines.
        for line in out.split("\n"):
            assert line == line.rstrip(), f"trailing whitespace on: {line!r}"
