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
ARGS_FILE="${ARGS_FILE:?}"
count=0
if [ -f "$COUNT_FILE" ]; then count="$(cat "$COUNT_FILE")"; fi
count=$((count+1))
echo "$count" > "$COUNT_FILE"
printf '%s\n' "$*" >> "$ARGS_FILE"
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
TIMEOUT_COUNT_FILE="${TIMEOUT_COUNT_FILE:-}"
timeout_count=0
if [ -n "$TIMEOUT_COUNT_FILE" ] && [ -f "$TIMEOUT_COUNT_FILE" ]; then timeout_count="$(cat "$TIMEOUT_COUNT_FILE")"; fi
if [ "${TIMEOUT_MODE:-pass}" = "always" ]; then
  exit 124
fi
if [ "${TIMEOUT_MODE:-pass}" = "first_only" ]; then
  timeout_count=$((timeout_count+1))
  if [ -n "$TIMEOUT_COUNT_FILE" ]; then echo "$timeout_count" > "$TIMEOUT_COUNT_FILE"; fi
  "$@"
  if [ "$timeout_count" -eq 1 ]; then
    exit 124
  fi
  exit $?
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
    args = tmp_path / "pull-args.txt"
    timeout_count = tmp_path / "timeout-count.txt"

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
            "ARGS_FILE": str(args),
            "TIMEOUT_COUNT_FILE": str(timeout_count),
            "SCENARIO": scenario,
            "GIT_DIRTY": git_dirty,
            "TIMEOUT_MODE": timeout_mode,
            "MAX_BATCHES": "10",
            "MAX_IDLE_HAS_MORE_BATCHES": "3",
            "BATCH_TIMEOUT_SECONDS": "1",
            "MAX_PAGES_PER_BATCH": "5",
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


def test_non_force_uses_max_pages_limit(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        MAX_PAGES_PER_BATCH="7",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip()
    assert args == "pull --max-pages 7"


def test_force_uses_force_flag_only(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        INPUT_FORCE="true",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip()
    assert args == "pull --force"


def test_non_force_includes_team_prefilter_args_when_present(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        MAX_PAGES_PER_BATCH="7",
        FIGMA_TEAM_ID="12345",
        SINCE="7d",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip()
    assert args == "pull --max-pages 7 --team-id 12345 --since 7d"


def test_timeout_retries_with_smaller_batch_then_succeeds(tmp_path: Path) -> None:
    out = _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="first_only",
        MAX_PAGES_PER_BATCH="8",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip().splitlines()
    assert args == ["pull --max-pages 8", "pull --max-pages 4"]
    assert "retrying with --max-pages 4" in out


def test_timeout_does_not_retry_in_force_mode(tmp_path: Path) -> None:
    out = _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="first_only",
        INPUT_FORCE="true",
    )
    count = int((tmp_path / "count.txt").read_text())
    assert count == 1
    assert "stopping checkpoint loop early." in out


def test_emits_sync_observability_logs_and_files(tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    out = _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
    )

    events = obs_dir / "checkpoint_events.csv"
    summary = obs_dir / "checkpoint_summary.txt"
    assert events.exists()
    assert summary.exists()

    events_text = events.read_text()
    assert "event" in events_text.splitlines()[0]
    assert "batch_start" in events_text
    assert "loop_end" in events_text

    summary_text = summary.read_text()
    assert "batches_started=" in summary_text
    assert "total_commits=1" in summary_text

    assert "SYNC_OBS event=batch_start" in out
    assert "SYNC_OBS summary_file=" in out


def test_timeout_stop_emits_batch_end_event(tmp_path: Path) -> None:
    obs_dir = tmp_path / "obs"
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="always",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
    )

    events_text = (obs_dir / "checkpoint_events.csv").read_text()
    assert "batch_start" in events_text
    assert "batch_timeout_stop" in events_text
    assert "batch_end" in events_text
