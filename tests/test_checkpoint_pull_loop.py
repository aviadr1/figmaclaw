"""Tests for scripts/checkpoint_pull_loop.sh guardrail behavior.

These tests stub `figmaclaw`, `git`, and `timeout` in PATH so we can verify loop
semantics deterministically without calling external services.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _setup_fake_bin(tmp_path: Path, *, scenario: str, git_dirty: str, timeout_mode: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_executable(
        bin_dir / "figmaclaw",
        """#!/usr/bin/env bash
set -euo pipefail
COUNT_FILE="${COUNT_FILE:?}"
count=0
if [ -f "$COUNT_FILE" ]; then count="$(cat "$COUNT_FILE")"; fi
count=$((count+1))
echo "$count" > "$COUNT_FILE"
echo "COMMIT_MSG:sync: figmaclaw — checkpoint batch $count"
case "${SCENARIO:?}" in
  has_more_forever) echo "HAS_MORE:true" ;;
  single_done) echo "HAS_MORE:false" ;;
  two_then_done)
    if [ "$count" -lt 2 ]; then echo "HAS_MORE:true"; else echo "HAS_MORE:false"; fi
    ;;
  *) echo "HAS_MORE:false" ;;
esac
""",
    )

    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
set -euo pipefail
TRACE_FILE="${TRACE_FILE:?}"
echo "git $*" >> "$TRACE_FILE"
if [ "${1:-}" = "diff" ] && [ "${GIT_DIRTY:-0}" = "1" ]; then
  exit 1
fi
exit 0
""",
    )

    _write_executable(
        bin_dir / "timeout",
        """#!/usr/bin/env bash
set -euo pipefail
_duration="${1:?}"
shift
if [ "${TIMEOUT_MODE:-pass}" = "always" ]; then
  exit 124
fi
"$@"
""",
    )

    return bin_dir


def _run_loop(
    tmp_path: Path, *, scenario: str, git_dirty: str, timeout_mode: str, **extra_env: str
) -> str:
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "checkpoint_pull_loop.sh"
    out_path = tmp_path / "figmaclaw-out.txt"
    trace = tmp_path / "git-trace.txt"
    count = tmp_path / "count.txt"

    bin_dir = _setup_fake_bin(
        tmp_path, scenario=scenario, git_dirty=git_dirty, timeout_mode=timeout_mode
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "COUNT_FILE": str(count),
            "TRACE_FILE": str(trace),
            "FIGMACLAW_OUT_PATH": str(out_path),
            "SCENARIO": scenario,
            "GIT_DIRTY": git_dirty,
            "TIMEOUT_MODE": timeout_mode,
            "MAX_BATCHES": "10",
            "MAX_IDLE_HAS_MORE_BATCHES": "3",
            "BATCH_TIMEOUT_SECONDS": "1",
            "INPUT_FORCE": "false",
        }
    )
    env.update(extra_env)

    result = subprocess.run(
        [str(script)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    return result.stdout + result.stderr


def test_stops_after_repeated_has_more_without_commits(tmp_path: Path) -> None:
    out = _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="0",  # git diff --cached --quiet => no commit
        timeout_mode="pass",
    )
    count = int((tmp_path / "count.txt").read_text())
    assert count == 3
    assert "Stopping loop after repeated HAS_MORE:true without progress." in out


def test_stops_immediately_on_pull_timeout(tmp_path: Path) -> None:
    out = _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="1",
        timeout_mode="always",  # timeout returns 124 before figmaclaw runs
    )
    assert (tmp_path / "count.txt").exists() is False
    assert "timed out" in out
    trace = (
        (tmp_path / "git-trace.txt").read_text() if (tmp_path / "git-trace.txt").exists() else ""
    )
    assert "git pull" not in trace


def test_force_mode_runs_single_batch_even_when_has_more(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="1",
        timeout_mode="pass",
        INPUT_FORCE="true",
    )
    count = int((tmp_path / "count.txt").read_text())
    assert count == 1


def test_continues_when_has_more_and_commits_then_stops_on_false(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="two_then_done",
        git_dirty="1",  # commit each batch
        timeout_mode="pass",
    )
    count = int((tmp_path / "count.txt").read_text())
    assert count == 2
