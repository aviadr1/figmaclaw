"""Tests for figmaclaw.budget (figmaclaw#26).

The adaptive budget is a pure function — these tests assert on its exact
outputs (decision, predicted/remaining seconds, and the stable ``reason``
string) for a set of scenarios that include the empirical replay of the
2026-04-05 create-community timeout incident.

INVARIANT: the ``reason`` string is part of the function contract. It is
grep-able in CI logs and pinned by :class:`TestGoldenLog`. Any refactor
that changes its format must update the golden test deliberately.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

from figmaclaw.budget import (
    _p75,
    decide_next_batch,
    load_per_frame_history,
)


class TestSyntaxValidity:
    """Canary test — broken budget module silently disables CI enrichment."""

    def test_budget_compiles(self) -> None:
        script = Path(__file__).parent.parent / "figmaclaw" / "budget.py"
        py_compile.compile(str(script), doraise=True)


class TestP75Helper:
    """_p75 uses inclusive nearest-rank — conservative, robust for small n."""

    def test_n1(self) -> None:
        assert _p75([4.0]) == 4.0

    def test_n2_returns_larger(self) -> None:
        assert _p75([2.0, 8.0]) == 8.0

    def test_n3_returns_max(self) -> None:
        # ceil(0.75 * 2) = 2 → index 2 → max
        assert _p75([3.0, 5.0, 9.0]) == 9.0

    def test_n4_returns_max(self) -> None:
        # ceil(0.75 * 3) = 3 → index 3 → max
        assert _p75([5.0, 6.0, 7.0, 8.0]) == 8.0

    def test_n5_returns_fourth_element(self) -> None:
        # ceil(0.75 * 4) = 3 → index 3 → fourth sorted element
        assert _p75([3.8, 5.8, 6.2, 7.4, 8.7]) == 7.4

    def test_unsorted_input(self) -> None:
        assert _p75([8.7, 3.8, 7.4, 5.8, 6.2]) == 7.4


class TestDecideColdStart:
    """With <2 history samples, use the cold-start per-frame estimate."""

    def test_empty_history_uses_cold_start(self) -> None:
        d = decide_next_batch(
            elapsed_seconds=0,
            planned_frames=80,
            per_frame_history=[],
        )
        # cold_start = 10 s/frame * 80 + 60 overhead = 860s
        # remaining = 3300 - 0 - 120 = 3180s
        assert d.should_start is True
        assert d.predicted_seconds == 860.0
        assert d.per_frame_estimate == 10.0
        assert d.history_used == 0
        assert "cold-start" in d.reason

    def test_single_sample_still_cold_start(self) -> None:
        d = decide_next_batch(
            elapsed_seconds=0,
            planned_frames=80,
            per_frame_history=[3.0],  # one sample is not enough
        )
        assert d.per_frame_estimate == 10.0
        assert "cold-start" in d.reason

    def test_cold_start_still_allows_conservative_first_batch(self) -> None:
        # Even with cold-start (10 s/frame), a 150-frame batch at t=0 fits.
        d = decide_next_batch(
            elapsed_seconds=0,
            planned_frames=150,
            per_frame_history=[],
        )
        assert d.should_start is True
        assert d.predicted_seconds == 1560.0


class TestDecideStopConditions:
    """The adaptive stop decision for near-end-of-budget scenarios."""

    def test_stops_when_predicted_exceeds_remaining(self) -> None:
        # Cancel-run replay: after 8 batches at 50.1m elapsed, next batch
        # of 80 frames with per-frame history from the real CSV data.
        d = decide_next_batch(
            elapsed_seconds=50.1 * 60,
            planned_frames=80,
            per_frame_history=[2.9, 3.3, 5.9, 6.5, 7.5],
        )
        # remaining = 3300 - 3006 - 120 = 174s
        # p75 of [2.9, 3.3, 5.9, 6.5, 7.5] (n=5, index 3) = 6.5
        # predicted = 80 * 6.5 + 60 = 580s
        # 580 > 174 → STOP
        assert d.should_start is False
        assert d.predicted_seconds == 580.0
        assert d.remaining_seconds == 174.0
        assert d.per_frame_estimate == 6.5
        assert d.history_used == 5
        assert "→ STOP" in d.reason

    def test_just_barely_fits(self) -> None:
        # Boundary: predicted == remaining is a GO (<= comparison).
        d = decide_next_batch(
            elapsed_seconds=0,
            planned_frames=10,
            per_frame_history=[1.0, 1.0, 1.0],
            hard_cap_seconds=200,
            shutdown_margin_seconds=120,
            fixed_overhead_seconds=60,
        )
        # remaining = 200 - 0 - 120 = 80
        # p75 = 1.0, predicted = 10 * 1.0 + 60 = 70
        # 70 <= 80 → GO
        assert d.should_start is True
        assert d.predicted_seconds == 70.0

    def test_remaining_can_be_negative_still_stops(self) -> None:
        # Elapsed already past the cap — remaining is negative, must stop.
        d = decide_next_batch(
            elapsed_seconds=60 * 60,
            planned_frames=10,
            per_frame_history=[2.0, 2.0, 2.0],
        )
        assert d.should_start is False
        assert d.remaining_seconds < 0


class TestDecideWithHistory:
    """Uses p75 of the last *history_window* samples when enough history."""

    def test_uses_only_last_window_samples(self) -> None:
        # 8 samples — only the last 5 matter by default.
        d = decide_next_batch(
            elapsed_seconds=0,
            planned_frames=10,
            per_frame_history=[100.0, 100.0, 100.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        )
        # window = last 5 = [2, 3, 4, 5, 6], p75 = index 3 = 5.0
        assert d.per_frame_estimate == 5.0
        assert d.history_used == 5

    def test_custom_window_size(self) -> None:
        d = decide_next_batch(
            elapsed_seconds=0,
            planned_frames=10,
            per_frame_history=[1.0, 2.0, 3.0, 4.0, 5.0],
            history_window=3,
        )
        # window = last 3 = [3, 4, 5], p75 of n=3 = max = 5.0
        assert d.per_frame_estimate == 5.0
        assert d.history_used == 3


class TestDecideReplaysRealRuns:
    """Regression tests that replay the two captured stream-json artifacts.

    These tests are the whole reason figmaclaw#26 exists. If they start
    failing, someone has changed the budget policy in a way that either
    (a) loses throughput on the success-path or
    (b) lets the dispatcher start a doomed batch that will be killed at 55m.
    """

    # Per-frame times extracted from the JSONL ``result`` events of
    # run 23996806327 (success) and 23995926981 (cancelled). Only the
    # section-mode "batch" shape is included — finalize batches have a
    # different unit and a different rolling window in the real dispatcher.
    SUCCESS_BATCHES = [
        # (planned_frames, wall_clock_seconds, per_frame_seconds)
        (80, 587.0, 7.4),  # b0
        (80, 693.0, 8.7),  # b1
        (80, 462.0, 5.8),  # b2
        (80, 499.0, 6.2),  # b3
        (35, 134.0, 3.8),  # b4
        # b5 of the success run is a finalize (different shape) — excluded.
    ]

    CANCEL_BATCHES = [
        (81, 515.0, 6.4),  # b0
        (80, 199.0, 2.5),  # b1
        (80, 237.0, 2.9),  # b2
        (48, 153.0, 3.3),  # b3
        # b4 of the cancel run is a finalize — excluded from per-frame stats
        # but still consumes wall-clock
        (81, 478.0, 5.9),  # b5
        (81, 604.0, 7.5),  # b6
        (81, 531.0, 6.5),  # b7
    ]

    INTER_BATCH_OVERHEAD = 5.0  # empirical ~5s for git pull/commit/push

    def _replay(
        self,
        batches: list[tuple[int, float, float]],
    ) -> tuple[list[bool], float, list[float]]:
        """Walk the batches in order, calling decide_next_batch before each.

        Returns (decisions, cumulative_elapsed_at_stop, per_frame_history).
        The decision for batch i is made with the history available *before*
        that batch runs — i.e. per-frame times from batches 0..i-1.
        """
        decisions: list[bool] = []
        history: list[float] = []
        elapsed = 0.0
        for planned, actual_s, per_frame in batches:
            d = decide_next_batch(
                elapsed_seconds=elapsed,
                planned_frames=planned,
                per_frame_history=history,
            )
            decisions.append(d.should_start)
            if not d.should_start:
                break
            elapsed += actual_s + self.INTER_BATCH_OVERHEAD
            history.append(per_frame)
        return decisions, elapsed, history

    def test_success_run_all_batches_allowed(self) -> None:
        """Every batch the real success run completed must be allowed by the policy."""
        decisions, _elapsed, _h = self._replay(self.SUCCESS_BATCHES)
        assert all(decisions), f"expected all GO, got {decisions}"

    def test_cancel_run_all_completed_batches_allowed(self) -> None:
        """Every batch the cancel run actually *completed* must be allowed.

        This protects against over-conservative budgets that would lose
        throughput on a run that was close to completion.
        """
        decisions, _elapsed, _h = self._replay(self.CANCEL_BATCHES)
        assert all(decisions), f"expected all GO for 7 completed batches, got {decisions}"

    def test_cancel_run_stops_before_hypothetical_9th_batch(self) -> None:
        """After the 7 completed batches of the cancel run, a hypothetical
        80-frame 9th batch must be refused — that's the doomed batch that
        got killed at 55m in the real run.
        """
        _decisions, elapsed, history = self._replay(self.CANCEL_BATCHES)
        # All 7 ran — now try to start a hypothetical 8th (80-frame) batch.
        d9 = decide_next_batch(
            elapsed_seconds=elapsed,
            planned_frames=80,
            per_frame_history=history,
        )
        assert d9.should_start is False, (
            f"budget must refuse the doomed batch — elapsed={elapsed:.0f}s "
            f"predicted={d9.predicted_seconds:.0f}s remaining={d9.remaining_seconds:.0f}s"
        )


class TestGoldenLog:
    """Pin the exact ``reason`` string format.

    Any refactor that changes the log format will break this test and
    force a deliberate update to both the code and the test. CI log
    consumers grep for ``[budget]`` — the format is load-bearing.
    """

    def test_go_reason_string_is_exact(self) -> None:
        d = decide_next_batch(
            elapsed_seconds=1000,
            planned_frames=50,
            per_frame_history=[5.0, 6.0, 7.0, 8.0],
        )
        # remaining = 3300 - 1000 - 120 = 2180
        # p75 of [5,6,7,8] = 8.0
        # predicted = 50 * 8 + 60 = 460
        # history rounded = [5.0, 6.0, 7.0, 8.0]
        expected = (
            "[budget] elapsed=1000s remaining=2180s "
            "(hard_cap=3300s - margin=120s) "
            "next=50 frames × 8.00s/frame (p75 of n=4) "
            "+ 60s overhead = predicted=460s "
            "→ GO "
            "history=[5.0, 6.0, 7.0, 8.0]"
        )
        assert d.reason == expected

    def test_stop_reason_string_is_exact(self) -> None:
        d = decide_next_batch(
            elapsed_seconds=3000,
            planned_frames=80,
            per_frame_history=[6.0, 7.0, 8.0, 9.0],
        )
        # remaining = 3300 - 3000 - 120 = 180
        # p75 of [6,7,8,9] = 9.0
        # predicted = 80 * 9 + 60 = 780
        expected = (
            "[budget] elapsed=3000s remaining=180s "
            "(hard_cap=3300s - margin=120s) "
            "next=80 frames × 9.00s/frame (p75 of n=4) "
            "+ 60s overhead = predicted=780s "
            "→ STOP "
            "history=[6.0, 7.0, 8.0, 9.0]"
        )
        assert d.reason == expected

    def test_cold_start_reason_string_is_exact(self) -> None:
        d = decide_next_batch(
            elapsed_seconds=0,
            planned_frames=20,
            per_frame_history=[],
        )
        # cold-start: per_frame = 10, predicted = 20*10 + 60 = 260
        # remaining = 3300 - 0 - 120 = 3180
        expected = (
            "[budget] elapsed=0s remaining=3180s "
            "(hard_cap=3300s - margin=120s) "
            "next=20 frames × 10.00s/frame (cold-start) "
            "+ 60s overhead = predicted=260s "
            "→ GO "
            "history=[]"
        )
        assert d.reason == expected


class TestLoadHistoryFromCSV:
    """Reading per-frame history from ``.figma-sync/enrichment-log.csv``."""

    def _write_csv(self, path: Path, rows: list[dict[str, str]]) -> None:
        """Write an enrichment-log CSV matching the _log_enrichment schema."""
        header = (
            "timestamp,file,mode,frames,duration_s,success,section,"
            "turns,cost_usd,claude_duration_ms,stop_reason"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [header]
        for r in rows:
            lines.append(
                f"{r.get('timestamp', '2026-04-05T00:00:00+00:00')},"
                f"{r.get('file', 'figma/x.md')},"
                f"{r.get('mode', 'batch')},"
                f"{r.get('frames', '80')},"
                f"{r.get('duration_s', '480')},"
                f"{r.get('success', 'True')},"
                f"{r.get('section', '')},"
                f"{r.get('turns', '15')},"
                f"{r.get('cost_usd', '1.2')},"
                f"{r.get('claude_duration_ms', '480000')},"
                f"{r.get('stop_reason', 'end_turn')}"
            )
        path.write_text("\n".join(lines) + "\n")

    def test_missing_csv_returns_empty(self, tmp_path: Path) -> None:
        assert load_per_frame_history(tmp_path / "nonexistent.csv", "batch") == []

    def test_reads_batch_rows_into_per_frame_times(self, tmp_path: Path) -> None:
        csv = tmp_path / "log.csv"
        self._write_csv(
            csv,
            [
                {"frames": "80", "duration_s": "480", "mode": "batch"},
                {"frames": "80", "duration_s": "560", "mode": "batch"},
                {"frames": "40", "duration_s": "200", "mode": "batch"},
            ],
        )
        h = load_per_frame_history(csv, "batch")
        assert h == [6.0, 7.0, 5.0]

    def test_filters_by_mode(self, tmp_path: Path) -> None:
        csv = tmp_path / "log.csv"
        self._write_csv(
            csv,
            [
                {"frames": "80", "duration_s": "480", "mode": "batch"},
                {"frames": "100", "duration_s": "900", "mode": "whole-page"},
                {"frames": "80", "duration_s": "400", "mode": "batch"},
            ],
        )
        assert load_per_frame_history(csv, "batch") == [6.0, 5.0]
        assert load_per_frame_history(csv, "whole-page") == [9.0]

    def test_skips_failed_rows(self, tmp_path: Path) -> None:
        csv = tmp_path / "log.csv"
        self._write_csv(
            csv,
            [
                {"frames": "80", "duration_s": "480", "mode": "batch", "success": "True"},
                {"frames": "80", "duration_s": "999", "mode": "batch", "success": "False"},
                {"frames": "80", "duration_s": "400", "mode": "batch", "success": "True"},
            ],
        )
        assert load_per_frame_history(csv, "batch") == [6.0, 5.0]

    def test_skips_rows_with_zero_frames(self, tmp_path: Path) -> None:
        # Stuck/error rows may have frames=0 — don't pollute the prior.
        csv = tmp_path / "log.csv"
        self._write_csv(
            csv,
            [
                {"frames": "0", "duration_s": "10", "mode": "batch"},
                {"frames": "80", "duration_s": "480", "mode": "batch"},
            ],
        )
        assert load_per_frame_history(csv, "batch") == [6.0]

    def test_honours_window_size(self, tmp_path: Path) -> None:
        csv = tmp_path / "log.csv"
        rows = [
            {"frames": "80", "duration_s": str(s), "mode": "batch"}
            for s in range(100, 1000, 100)  # 9 rows
        ]
        self._write_csv(csv, rows)
        h = load_per_frame_history(csv, "batch", window=3)
        # Last 3 durations = 700, 800, 900 → per-frame = 8.75, 10.0, 11.25
        assert h == [8.75, 10.0, 11.25]

    def test_skips_malformed_rows(self, tmp_path: Path) -> None:
        csv = tmp_path / "log.csv"
        self._write_csv(
            csv,
            [
                {"frames": "abc", "duration_s": "480", "mode": "batch"},
                {"frames": "80", "duration_s": "xyz", "mode": "batch"},
                {"frames": "80", "duration_s": "480", "mode": "batch"},
            ],
        )
        assert load_per_frame_history(csv, "batch") == [6.0]


def test_load_history_reads_schema_v1_header_rows(tmp_path: Path) -> None:
    """INVARIANT: load_per_frame_history works with schema-v1 enrichment log header."""
    csv_path = tmp_path / "log.csv"
    csv_path.write_text(
        "schema_version,event_id,run_id,timestamp,file,mode,frames,duration_s,success,section,turns,cost_usd,claude_duration_ms,stop_reason\n"
        "1,e1,run-a,2026-04-15T00:00:00+00:00,figma/a.md,batch,80,480,True,Auth,2,0.1111,50000,end_turn\n"
        "1,e2,run-a,2026-04-15T00:01:00+00:00,figma/a.md,batch,80,999,False,Auth,2,0.1111,50000,error\n"
        "1,e3,run-b,2026-04-15T00:02:00+00:00,figma/a.md,whole-page,100,900,True,,2,0.2222,90000,end_turn\n"
        "1,e4,run-b,2026-04-15T00:03:00+00:00,figma/a.md,batch,80,400,True,Auth,2,0.1111,50000,end_turn\n",
        encoding="utf-8",
    )

    assert load_per_frame_history(csv_path, "batch") == [6.0, 5.0]
    assert load_per_frame_history(csv_path, "whole-page") == [9.0]
