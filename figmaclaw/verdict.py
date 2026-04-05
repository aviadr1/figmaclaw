"""Run-conclusion verdict for ``claude-run`` (figmaclaw#27).

This module decides whether a ``claude-run`` invocation should exit GREEN
or RED based on six observable counters gathered by the dispatcher:

    files_selected      — how many files did ``collect_files`` return
    work_attempted      — how many Claude invocations were actually started
    commits_made        — how many commits landed between start_sha and HEAD
    errors              — subprocess non-zero + dispatch exceptions
    budget_exhausted    — did the figmaclaw#26 adaptive self-limit fire
    skipped_no_work     — files the selector picked but the dispatcher
                          found no pending work for (phantom selection)

The output is a **pure function** of those counters. Same inputs → same
verdict string → same exit code, byte-for-byte reproducible in tests and CI
step summaries. Golden tests pin the verdict strings so future refactors
can't silently change the semantics.

Decision rows (see figmaclaw#27 for full rationale):

    1  selector empty OR all files raced to enriched  → GREEN (no-op)
    2  work attempted, commits landed, no errors      → GREEN (clean)
    3  budget_exhausted=True with ≥1 commit           → GREEN (budget-limited)
    4a minority errors (≤50%), ≥1 commit              → GREEN (partial)
    4b majority errors (>50%)                         → RED (majority failure)
    5  skipped_no_work > 0                            → RED (phantom selection)
    6  work attempted, 0 commits, 0 errors            → RED (silent dispatch)
    7  work attempted, 0 commits, errors > 0          → RED (classic failure)
    8  unhandled exception                            → RED (crash, handled
                                                            by the caller)

Row 1 covers two scenarios: (a) ``collect_files`` returned zero files,
and (b) every selected file was re-checked and found already-enriched
(benign race with a concurrent run). Both are legitimate no-ops —
``work_attempted == 0 and skipped_no_work == 0`` is the signature.

Row 5 is evaluated **first** and wins over every other row: a single
phantom-selected file makes the run RED even if other files succeeded.
This is deliberate — selector/dispatcher disagreement is always a bug and
should never be hidden by unrelated success on other files.

See also ``figmaclaw.budget`` (figmaclaw#26), which produces the
``budget_exhausted`` flag that distinguishes row 3 from row 2.
"""

from __future__ import annotations

import os
from pathlib import Path

import pydantic

# Exit codes — two values only. No "soft red" nonsense.
EXIT_GREEN = 0
EXIT_RED = 2


class RunVerdict(pydantic.BaseModel):
    """Verdict returned by :func:`compute_verdict`.

    The ``label`` string is part of the contract — tests assert on it,
    the CI step summary prints it, and the exit code must match (every
    "RED" label pairs with exit 2, every "GREEN" label with exit 0).

    Frozen (immutable) by convention: the repo-wide rule is to prefer pydantic
    over dataclass for structured values (see ``CLAUDE.md`` → *Conventions*).
    """

    model_config = pydantic.ConfigDict(frozen=True)

    label: str
    exit_code: int
    row: str  # human-readable row identifier, e.g. "row 3", "row 5"


def compute_verdict(
    *,
    files_selected: int,
    work_attempted: int,
    commits_made: int,
    errors: int,
    budget_exhausted: bool,
    skipped_no_work: int,
) -> RunVerdict:
    """Decide the run verdict from the six observable counters.

    This function is pure — no I/O, no clock, no environment reads. The
    mapping from counter tuple to verdict is deterministic and the output
    ``label`` is stable enough for golden-log tests to pin it.

    Row 5 (phantom selection) is evaluated first. It wins over every other
    row: even if 9 files succeeded and 1 was phantom-skipped, the run is
    RED. This is intentional — a single selector/dispatcher disagreement
    is always a bug and must never be hidden behind unrelated success.

    Row 8 (crash) is not handled here. The caller wraps the dispatch loop
    in a try/except and sets the exit code to :data:`EXIT_RED` directly
    on any unhandled exception.
    """
    # Row 5 — ALWAYS wins. Phantom selection is never defeasible.
    if skipped_no_work > 0:
        return RunVerdict(
            label="RED (phantom selection)",
            exit_code=EXIT_RED,
            row="row 5",
        )

    # Row 1 — nothing to do. Covers both "selector returned empty" and
    # "every selected file was already-enriched by a concurrent run at
    # re-check time" (benign race). work_attempted == 0 without any
    # phantom-selection is the signature of the race case.
    if files_selected == 0 or work_attempted == 0:
        return RunVerdict(
            label="GREEN (no-op)",
            exit_code=EXIT_GREEN,
            row="row 1",
        )

    # Row 6/7 — tried but no commits landed
    if commits_made == 0:
        if errors > 0:
            return RunVerdict(
                label="RED (failure — no commits, errors present)",
                exit_code=EXIT_RED,
                row="row 7",
            )
        return RunVerdict(
            label="RED (silent dispatch failure — no commits, no errors)",
            exit_code=EXIT_RED,
            row="row 6",
        )

    # Row 4b — majority errors is red even if some commits landed
    if work_attempted > 0 and errors / work_attempted > 0.5:
        return RunVerdict(
            label="RED (majority failure)",
            exit_code=EXIT_RED,
            row="row 4b",
        )

    # Row 3 — budget-limited partial progress
    if budget_exhausted:
        return RunVerdict(
            label="GREEN (budget-limited partial progress)",
            exit_code=EXIT_GREEN,
            row="row 3",
        )

    # Row 4a — minority errors but mostly successful
    if errors > 0:
        return RunVerdict(
            label="GREEN (partial, minority errors)",
            exit_code=EXIT_GREEN,
            row="row 4a",
        )

    # Row 2 — clean completion
    return RunVerdict(
        label="GREEN (clean completion)",
        exit_code=EXIT_GREEN,
        row="row 2",
    )


def format_step_summary(
    *,
    verdict: RunVerdict,
    files_selected: int,
    work_attempted: int,
    commits_made: int,
    errors: int,
    budget_exhausted: bool,
    skipped_no_work: int,
    phantom_files: list[str] | None = None,
    budget_stop_reason: str | None = None,
) -> str:
    """Render the GitHub Actions step summary for a completed run.

    Returns a Markdown string suitable for appending to the file named in
    ``$GITHUB_STEP_SUMMARY``. The output is stable enough for snapshot
    tests to pin it.

    The ``phantom_files`` list, when the verdict is row 5, names each
    file the dispatcher identified as phantom-selected. The
    ``budget_stop_reason`` is the ``[budget]`` line that triggered the
    row 3 stop, when applicable.
    """
    lines: list[str] = []
    lines.append("## claude-run summary")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| files_selected | {files_selected} |")
    lines.append(f"| work_attempted | {work_attempted} |")
    lines.append(f"| commits_made | {commits_made} |")
    lines.append(f"| errors | {errors} |")
    lines.append(f"| budget_exhausted | {str(budget_exhausted).lower()} |")
    lines.append(f"| skipped_no_work | {skipped_no_work} |")
    lines.append("")
    lines.append(f"**Verdict ({verdict.row}): {verdict.label}**")
    lines.append("")
    if phantom_files:
        lines.append("### Phantom-selected files (selector/dispatcher disagreement)")
        lines.append("")
        for path in phantom_files:
            lines.append(
                f"- `{path}` — selector picked this file but the "
                f"dispatcher found no pending work",
            )
        lines.append("")
    if budget_stop_reason:
        lines.append("### Budget stop")
        lines.append("")
        lines.append("```")
        lines.append(budget_stop_reason)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def write_step_summary(summary: str) -> None:
    """Append the rendered summary to ``$GITHUB_STEP_SUMMARY`` if set.

    No-op outside of GitHub Actions. This is the only function in this
    module that touches the environment; it's kept separate from the pure
    :func:`format_step_summary` so tests can exercise the formatter
    without any file I/O.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(summary)
            if not summary.endswith("\n"):
                f.write("\n")
    except OSError:
        # Step summary is a nice-to-have, not load-bearing for the exit code.
        pass


def count_commits_since(start_sha: str, repo_dir: Path | None = None) -> int:
    """Count commits from *start_sha* to ``HEAD``.

    Returns 0 if the rev-list call fails (detached HEAD, missing sha, git
    not on PATH). This is intentional: ``commits_made == 0`` is a signal
    the verdict function already handles correctly, and raising here would
    turn a diagnostic failure into a crash.
    """
    import subprocess

    if not start_sha:
        return 0
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{start_sha}..HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_dir) if repo_dir is not None else None,
        )
    except (OSError, FileNotFoundError):
        return 0
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0


def head_sha(repo_dir: Path | None = None) -> str:
    """Return the current HEAD commit sha, or empty string on failure."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_dir) if repo_dir is not None else None,
        )
    except (OSError, FileNotFoundError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
