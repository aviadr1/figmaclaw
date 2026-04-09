"""Adaptive time budget for ``claude-run`` (figmaclaw#26).

This module answers one question: *should the dispatcher start the next Claude
batch, or stop cleanly before the CI job hits its hard timeout?*

The answer is a **pure function** of observable inputs — elapsed wall-clock,
the next batch's planned frame count, and the recent per-frame history from
completed batches. No clock, no I/O, no globals. Same inputs → same decision
→ same reason string, byte-for-byte reproducible in tests and CI logs.

Why per-frame rather than per-batch wall-clock?  Empirical analysis of
stream-json artifacts from ``claude-run-raw-*-large`` (see figmaclaw#26 for
the full write-up) showed:

* Batches with the same ``total_pending`` frame count vary in wall-clock by
  up to 3.5× due to frame complexity and rate-limit pressure.
* Per-frame time is much more stable across batches: median 6.2 s/frame,
  p75 7.4 s/frame, max 8.7 s/frame across 11 bulk batches from two runs.
* The dispatcher knows the planned frame count *before* each Claude call —
  ``total_pending`` for section-mode batch, ``frame_count`` for whole-page
  or finalize — so it can price a batch before committing to it.

The policy is therefore:

    predicted_next = planned_frames * p75(last_N_per_frame_times) + fixed_overhead
    start_batch if predicted_next <= (hard_cap - elapsed - shutdown_margin)

When there is insufficient history for a meaningful p75 (fewer than
``min_history`` samples), a conservative ``cold_start_per_frame`` estimate is
used instead. This matters for the first batch of a fresh run or the first
batch in a new mode.

See also ``figmaclaw.verdict`` (figmaclaw#27), which consumes the
``budget_exhausted`` flag that this module sets when it decides to stop.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pydantic


class BudgetDecision(pydantic.BaseModel):
    """Decision returned by :func:`decide_next_batch`.

    The ``reason`` string is part of the contract — tests assert on it and
    CI log consumers grep for it. Its format must not change without updating
    the corresponding golden-log test.

    Frozen (immutable) by convention: the repo-wide rule is to prefer pydantic
    over dataclass for structured values (see ``CLAUDE.md`` → *Conventions*).
    """

    model_config = pydantic.ConfigDict(frozen=True)

    should_start: bool
    reason: str
    predicted_seconds: float
    remaining_seconds: float
    per_frame_estimate: float
    history_used: int


def _p75(values: list[float]) -> float:
    """75th percentile of a small sample, robust for n<=5.

    Uses the *inclusive* nearest-rank method: the value at index
    ``ceil(0.75 * (n-1))`` of the sorted list. For n=4 this is the 3rd
    element (index 2, 75% mark); for n=5 it's the 4th (index 3).
    """
    if not values:
        raise ValueError("_p75 requires at least one value")
    s = sorted(values)
    # ceil(0.75 * (n-1)) — keeps the result inside [0, n-1]
    idx = -(-3 * (len(s) - 1) // 4)
    return s[idx]


def decide_next_batch(
    *,
    elapsed_seconds: float,
    planned_frames: int,
    per_frame_history: list[float],
    hard_cap_seconds: float = 55 * 60,
    shutdown_margin_seconds: float = 120.0,
    fixed_overhead_seconds: float = 60.0,
    cold_start_per_frame: float = 10.0,
    history_window: int = 5,
    min_history: int = 2,
) -> BudgetDecision:
    """Decide whether to start the next Claude batch.

    Args:
        elapsed_seconds: Wall-clock since the dispatcher started, in seconds.
        planned_frames: Number of frames/units the next batch will process.
            For section-mode batches this is ``total_pending``; for whole-page
            enrichment this is the page's ``frame_count``; for finalize calls
            it's also ``frame_count``.
        per_frame_history: Seconds-per-frame values from recent *successful*
            batches in the *same mode*, oldest first. Only the last
            ``history_window`` entries are considered.
        hard_cap_seconds: The CI job's hard timeout (default 55 minutes, which
            matches ``claude-run.yml``'s ``timeout-minutes: 55``).
        shutdown_margin_seconds: Time reserved at the end of the run for
            git-push, stream flush, and job teardown. Empirically ≤30 s in
            observed runs; 120 s is 4× headroom.
        fixed_overhead_seconds: Per-batch fixed cost independent of frame
            count (subagent spawn, git pull before the batch). ~60 s covers
            observed overheads.
        cold_start_per_frame: Per-frame estimate used when history is too
            sparse for a stable p75. Pinned at 10 s — above the observed
            max of 8.7 s/frame — so cold starts are conservative.
        history_window: Keep only the most recent N samples for p75.
        min_history: Minimum samples required to compute p75. Below this, the
            cold-start estimate is used.

    Returns:
        A :class:`BudgetDecision` whose ``should_start`` tells the dispatcher
        what to do and whose ``reason`` is an exact, stable, loggable string.

    The function is pure: no clock reads, no I/O, no global state. Callers
    pass ``elapsed_seconds`` from ``time.monotonic() - run_start`` and
    ``per_frame_history`` from :func:`load_per_frame_history`.
    """
    remaining = hard_cap_seconds - elapsed_seconds - shutdown_margin_seconds
    window = list(per_frame_history[-history_window:])
    if len(window) < min_history:
        per_frame = cold_start_per_frame
        history_source = "cold-start"
    else:
        per_frame = _p75(window)
        history_source = f"p75 of n={len(window)}"
    predicted = planned_frames * per_frame + fixed_overhead_seconds
    fits = predicted <= remaining
    verb = "GO" if fits else "STOP"
    samples = [round(x, 1) for x in window]
    reason = (
        f"[budget] elapsed={elapsed_seconds:.0f}s "
        f"remaining={remaining:.0f}s "
        f"(hard_cap={hard_cap_seconds:.0f}s - margin={shutdown_margin_seconds:.0f}s) "
        f"next={planned_frames} frames × {per_frame:.2f}s/frame ({history_source}) "
        f"+ {fixed_overhead_seconds:.0f}s overhead = predicted={predicted:.0f}s "
        f"→ {verb} "
        f"history={samples}"
    )
    return BudgetDecision(
        should_start=fits,
        reason=reason,
        predicted_seconds=float(predicted),
        remaining_seconds=float(remaining),
        per_frame_estimate=float(per_frame),
        history_used=len(window),
    )


def load_per_frame_history(csv_path: Path, mode: str, window: int = 5) -> list[float]:
    """Read the per-frame time history for *mode* from the enrichment log.

    Returns seconds-per-frame values from the last *window* **successful**
    rows whose ``mode`` column matches *mode*, oldest first.

    The enrichment CSV (``.figma-sync/enrichment-log.csv``) is written by
    ``_log_enrichment`` in ``claude_run.py``. Columns used:

        mode            — "batch", "finalize", or "whole-page"
        frames          — planned frame count at the time of the batch
        duration_s      — wall-clock measured by the dispatcher
        success         — "True" / "False"

    Rows with ``success != True`` or ``frames <= 0`` are skipped: we want a
    prior on *healthy* batch throughput, not on failures. Rows with a
    non-numeric duration are skipped silently (should not happen in normal
    operation but defensive I/O is cheap).

    Missing file → empty list (cold start).
    """
    if not csv_path.exists():
        return []
    out: list[float] = []
    try:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("mode") != mode:
                    continue
                if row.get("success") != "True":
                    continue
                try:
                    frames = int(row.get("frames") or 0)
                    duration_s = float(row.get("duration_s") or 0)
                except (TypeError, ValueError):
                    continue
                if frames <= 0 or duration_s <= 0:
                    continue
                out.append(duration_s / frames)
    except OSError:
        return []
    return out[-window:]
