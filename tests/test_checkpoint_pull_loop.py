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
  auto_commit_then_has_more)
    echo "  ✓ committed: page"
    echo "HAS_MORE:true"
    ;;
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
if [ "${1:-}" = "push" ] && [ "${GIT_PUSH_FAIL:-0}" = "1" ]; then
  exit 1
fi
if [ "${1:-}" = "push" ] && [ "${GIT_PUSH_FAIL_ONCE:-0}" = "1" ]; then
  push_count_file="${GIT_PUSH_COUNT_FILE:?}"
  push_count=0
  if [ -f "$push_count_file" ]; then push_count="$(cat "$push_count_file")"; fi
  push_count=$((push_count+1))
  echo "$push_count" > "$push_count_file"
  if [ "$push_count" -eq 1 ]; then
    exit 1
  fi
fi
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
if [ -n "${TIMEOUT_ARGS_FILE:-}" ]; then
  printf '%s\\n' "$*" >> "$TIMEOUT_ARGS_FILE"
fi
if [[ "${1:-}" == --kill-after=* ]]; then
  shift
elif [ "${1:-}" = "--kill-after" ]; then
  shift 2
fi
_duration="${1:?}"
shift
TIMEOUT_COUNT_FILE="${TIMEOUT_COUNT_FILE:-}"
timeout_count=0
if [ -n "$TIMEOUT_COUNT_FILE" ] && [ -f "$TIMEOUT_COUNT_FILE" ]; then timeout_count="$(cat "$TIMEOUT_COUNT_FILE")"; fi
if [ "${TIMEOUT_MODE:-pass}" = "always" ]; then
  exit 124
fi
if [ "${TIMEOUT_MODE:-pass}" = "killed" ]; then
  exit 137
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
    timeout_args = tmp_path / "timeout-args.txt"
    timeout_count = tmp_path / "timeout-count.txt"
    push_count = tmp_path / "push-count.txt"

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
            "TIMEOUT_ARGS_FILE": str(timeout_args),
            "TIMEOUT_COUNT_FILE": str(timeout_count),
            "GIT_PUSH_COUNT_FILE": str(push_count),
            "SCENARIO": scenario,
            "GIT_DIRTY": git_dirty,
            "GIT_PUSH_FAIL": "0",
            "GIT_PUSH_FAIL_ONCE": "0",
            "TIMEOUT_MODE": timeout_mode,
            "MAX_BATCHES": "10",
            "MAX_IDLE_HAS_MORE_BATCHES": "3",
            "BATCH_TIMEOUT_SECONDS": "1",
            "MAX_PAGES_PER_BATCH": "5",
            "INPUT_FORCE": "false",
            "TARGET_REF": "main",
            # Default off in the test harness so existing expected-args assertions
            # (e.g. "pull --max-pages 7") stay stable; individual tests flip this
            # on when they want to exercise the auto-commit wiring.
            "AUTO_COMMIT_ENABLED": "false",
            "PUSH_EVERY": "1",
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


def test_commit_safety_net_pulls_configured_target_ref(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        TARGET_REF="test/figmaclaw-pr-129-ci",
    )
    trace = (tmp_path / "git-trace.txt").read_text()
    assert "git pull --no-rebase --ff-only origin test/figmaclaw-pr-129-ci" in trace
    assert "git pull --no-rebase --ff-only origin main" not in trace


def test_stops_immediately_on_pull_timeout(tmp_path: Path) -> None:
    out = _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="0",  # nothing to commit on timeout → no git pull either
        timeout_mode="always",  # timeout returns 124 before figmaclaw runs
    )
    assert (tmp_path / "count.txt").exists() is False
    assert "timed out" in out
    assert "with no dirty progress" in out
    assert "retrying with --max-pages" not in out
    trace = (
        (tmp_path / "git-trace.txt").read_text() if (tmp_path / "git-trace.txt").exists() else ""
    )
    # With no dirty state, the timeout path must not attempt to commit, so no
    # `git pull` / `git add` / `git commit` should be traced.
    assert "git commit" not in trace


def test_default_batch_timeout_fires_before_hosted_runner_shutdown() -> None:
    """INVARIANT: shell timeout must fire before GitHub cancels the step.

    Linear-git runs proved hosted runners can deliver a shutdown signal around
    5-6 minutes into the checkpointed pull step. The loop timeout must be lower
    so the script can flush progress and write observability before that.
    """
    script = (Path(__file__).parents[1] / "scripts" / "checkpoint_pull_loop.sh").read_text(
        encoding="utf-8"
    )

    assert 'BATCH_TIMEOUT_SECONDS="${BATCH_TIMEOUT_SECONDS:-240}"' in script
    assert 'TIMEOUT_KILL_AFTER_SECONDS="${TIMEOUT_KILL_AFTER_SECONDS:-30}"' in script
    assert 'timeout --kill-after="${TIMEOUT_KILL_AFTER_SECONDS}s"' in script


def test_timeout_passes_kill_after_to_coreutils_timeout(tmp_path: Path) -> None:
    """The timeout must be a hard boundary, not a best-effort SIGTERM.

    GNU timeout waits indefinitely after SIGTERM unless --kill-after is set.
    Live CI proved figmaclaw can stay stuck until a hosted-runner shutdown, so
    the reusable loop must always give timeout a SIGKILL deadline.
    """
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="0",
        timeout_mode="pass",
        TIMEOUT_KILL_AFTER_SECONDS="11",
    )
    timeout_args = (tmp_path / "timeout-args.txt").read_text()
    assert "--kill-after=11s" in timeout_args


def test_timeout_status_137_is_treated_as_batch_timeout(tmp_path: Path) -> None:
    """If timeout has to SIGKILL the child it exits 137; handle it like 124."""
    out = _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="0",
        timeout_mode="killed",
    )
    assert "timed out" in out
    assert "with no dirty progress" in out


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


def test_default_per_file_timeout_fires_before_batch_timeout(tmp_path: Path) -> None:
    """INVARIANT ERR-2/F24: one slow file must not consume the whole batch.

    The shell timeout is still the hard outer guard, but the Python command
    should receive a smaller per-file timeout so it can emit a scoped
    file_timeout event and continue to unrelated files.
    """
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        BATCH_TIMEOUT_SECONDS="240",
        TIMEOUT_KILL_AFTER_SECONDS="30",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip()
    assert args == "pull --max-pages 5 --per-file-timeout-s 180"


def test_configured_per_file_timeout_is_threaded_to_pull(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        PER_FILE_TIMEOUT_SECONDS="90",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip()
    assert args == "pull --max-pages 5 --per-file-timeout-s 90"


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
        BATCH_TIMEOUT_SECONDS="240",
        TIMEOUT_KILL_AFTER_SECONDS="30",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
    )

    events = obs_dir / "checkpoint_events.csv"
    summary = obs_dir / "checkpoint_summary.txt"
    invocations = obs_dir / "pull_invocations.txt"
    batch_log = obs_dir / "pull_batch_1.log"
    assert events.exists()
    assert summary.exists()
    assert invocations.exists()
    assert batch_log.exists()

    events_text = events.read_text()
    assert "event" in events_text.splitlines()[0]
    assert "batch_start" in events_text
    assert "loop_end" in events_text

    summary_text = summary.read_text()
    assert "batches_started=" in summary_text
    assert "total_commits=1" in summary_text

    invocations_text = invocations.read_text()
    assert "batch=1" in invocations_text
    assert "figmaclaw pull" in invocations_text
    assert "--per-file-timeout-s" in invocations_text

    batch_log_text = batch_log.read_text()
    assert "COMMIT_MSG:sync: figmaclaw" in batch_log_text
    assert "HAS_MORE:false" in batch_log_text

    assert "SYNC_OBS event=batch_start" in out
    assert "SYNC_OBS summary_file=" in out


def test_timeout_commits_partial_progress_when_tree_dirty(tmp_path: Path) -> None:
    """On timeout with a dirty tree, the loop must commit partial progress before
    retrying or stopping. Without this, the next CI run does a fresh checkout and
    all work done before SIGKILL is discarded."""
    out = _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="1",
        timeout_mode="always",
    )
    trace = (tmp_path / "git-trace.txt").read_text()
    # Must commit partial progress before giving up.
    assert "git commit" in trace
    # Message tag makes the commit recognizable in git log and distinct from
    # normal `checkpoint batch` commits.
    assert "partial progress" in trace
    # And still produces the timeout-stop observability event.
    assert "timed out" in out
    assert "stopping checkpoint loop early" in out


def test_timeout_backoff_commits_partial_progress_and_retries(tmp_path: Path) -> None:
    """On timeout with dirty tree AND backoff eligibility, the loop must commit
    before retrying with a smaller batch size."""
    out = _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="first_only",
        MAX_PAGES_PER_BATCH="8",
    )
    trace = (tmp_path / "git-trace.txt").read_text()
    # Two commits expected: one partial-progress after the timed-out batch 1,
    # one normal after the successful batch 2.
    assert trace.count("git commit") == 2
    assert "partial progress" in trace
    # Retry happened at halved batch size.
    assert "retrying with --max-pages 4" in out


def test_auto_commit_appends_flags_to_pull_args(tmp_path: Path) -> None:
    """With AUTO_COMMIT_ENABLED=true, the loop must pass --auto-commit and
    --push-every to figmaclaw so individual pages become durable in origin as
    they're written — making the batch-level timeout no longer a data-loss risk
    for already-processed pages."""
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        AUTO_COMMIT_ENABLED="true",
        PUSH_EVERY="1",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip()
    assert "--auto-commit" in args
    assert "--push-every 1" in args


def test_auto_commit_respects_custom_push_every(tmp_path: Path) -> None:
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        AUTO_COMMIT_ENABLED="true",
        PUSH_EVERY="5",
    )
    args = (tmp_path / "pull-args.txt").read_text().strip()
    assert "--push-every 5" in args


def test_auto_commit_also_flushes_unpushed_commits_on_timeout(tmp_path: Path) -> None:
    """With --push-every > 1, a SIGKILL can leave local page commits unpushed.
    On timeout the loop must explicitly git push before any safety-net commit
    so those page commits reach origin before the job ends."""
    _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="1",
        timeout_mode="always",
        AUTO_COMMIT_ENABLED="true",
        PUSH_EVERY="5",
    )
    trace = (tmp_path / "git-trace.txt").read_text()
    # Push must run independently of commit_if_changed's trailing push —
    # even a no-op local tree shouldn't suppress it, since --auto-commit
    # may have committed pages that aren't pushed yet.
    assert "git push" in trace


def test_timeout_treats_flushed_auto_commit_as_progress(tmp_path: Path) -> None:
    """A timeout after figmaclaw's page commit but before process completion is progress.

    The shell safety-net dirty check sees a clean tree in this shape, because the
    page was already committed by Python. It must not classify that as wasted
    work and stop the loop.
    """
    obs_dir = tmp_path / "obs"
    out = _run_loop(
        tmp_path,
        scenario="auto_commit_then_has_more",
        git_dirty="0",
        timeout_mode="first_only",
        MAX_PAGES_PER_BATCH="8",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
        AUTO_COMMIT_ENABLED="true",
        PUSH_EVERY="1",
    )

    events_text = (obs_dir / "checkpoint_events.csv").read_text()
    assert "partial_commit_on_timeout" in events_text
    assert "timeout after auto-committed progress" in events_text
    assert "retrying with --max-pages 4" in out


def test_timeout_counts_auto_commit_progress_even_when_flush_push_fails(
    tmp_path: Path,
) -> None:
    """Regression from linear-git CI.

    `figmaclaw pull --auto-commit --push-every 1` can push many page commits
    before the outer batch timeout fires. A final best-effort `git push` may
    still fail because origin already moved, but that must not make the wrapper
    report `total_commits=0` or stop as `timeout_no_progress_stop`.
    """
    obs_dir = tmp_path / "obs"
    out = _run_loop(
        tmp_path,
        scenario="auto_commit_then_has_more",
        git_dirty="0",
        timeout_mode="first_only",
        MAX_PAGES_PER_BATCH="8",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
        AUTO_COMMIT_ENABLED="true",
        PUSH_EVERY="1",
        GIT_PUSH_FAIL="1",
    )

    events_text = (obs_dir / "checkpoint_events.csv").read_text()
    summary_text = (obs_dir / "checkpoint_summary.txt").read_text()
    assert "auto_commit_progress" in events_text
    assert "auto_commit_flush_failed" in events_text
    assert "partial_commit_on_timeout" in events_text
    assert "timeout_no_progress_stop" not in events_text
    assert "total_auto_commits=10" in summary_text
    assert "total_commits=10" in summary_text
    assert "retrying with --max-pages 4" in out


def test_safety_net_commit_push_race_retries_without_aborting(tmp_path: Path) -> None:
    """A transient remote ref race during the shell safety-net push should not
    abort the wrapper before observability is written."""
    obs_dir = tmp_path / "obs"
    out = _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="1",
        timeout_mode="pass",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
        GIT_PUSH_FAIL_ONCE="1",
    )

    trace = (tmp_path / "git-trace.txt").read_text()
    summary_text = (obs_dir / "checkpoint_summary.txt").read_text()
    assert trace.count("git push") >= 2
    assert "git pull --no-rebase --ff-only origin main" in trace
    assert "total_safety_net_commits=1" in summary_text
    assert "final_reason=has_more_false" in summary_text
    assert "warning: safety-net commit push failed" not in out


def test_exit_trap_flushes_any_unpushed_commits_on_normal_exit(tmp_path: Path) -> None:
    """Normal-exit path must still `git push` once via the EXIT trap. With
    --auto-commit this drains any per-page commits that weren't pushed (e.g.
    PUSH_EVERY > 1 leaving a tail of unpushed commits, or a SIGKILL between
    Python's `git commit` and `git push`). Without this, the next CI run's
    fresh checkout discards those local commits."""
    _run_loop(
        tmp_path,
        scenario="single_done",
        git_dirty="0",
        timeout_mode="pass",
        AUTO_COMMIT_ENABLED="true",
        PUSH_EVERY="5",
    )
    trace = (tmp_path / "git-trace.txt").read_text()
    assert "git push" in trace, (
        "EXIT trap must issue at least one `git push` even when commit_if_changed "
        "returned false, to flush unpushed --auto-commit page commits."
    )


def test_exit_trap_does_not_fail_script_when_push_fails(tmp_path: Path) -> None:
    """A transient `git push` failure during the trap must not crash the script
    and mask the real exit status / observability output."""
    bin_dir = _setup_fake_bin(tmp_path, scenario="single_done", git_dirty="0", timeout_mode="pass")
    # Overwrite the helper's git stub with one that fails on `push` — simulates
    # network hiccup / auth token expired / remote conflict during the trap.
    (bin_dir / "git").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "push" ]; then exit 1; fi\n'
        'if [ "${1:-}" = "diff" ] && [ "${GIT_DIRTY:-0}" = "1" ]; then exit 1; fi\n'
        "exit 0\n"
    )
    (bin_dir / "git").chmod(0o755)

    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "checkpoint_pull_loop.sh"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "COUNT_FILE": str(tmp_path / "count.txt"),
            "TRACE_FILE": str(tmp_path / "git-trace.txt"),
            "FIGMACLAW_OUT_PATH": str(tmp_path / "figmaclaw-out.txt"),
            "ARGS_FILE": str(tmp_path / "pull-args.txt"),
            "TIMEOUT_COUNT_FILE": str(tmp_path / "timeout-count.txt"),
            "SCENARIO": "single_done",
            "GIT_DIRTY": "0",
            "TIMEOUT_MODE": "pass",
            "MAX_BATCHES": "10",
            "MAX_IDLE_HAS_MORE_BATCHES": "3",
            "BATCH_TIMEOUT_SECONDS": "1",
            "MAX_PAGES_PER_BATCH": "5",
            "INPUT_FORCE": "false",
            "AUTO_COMMIT_ENABLED": "true",
            "PUSH_EVERY": "1",
        }
    )
    # check=True catches nonzero exit — we expect exit 0 even with the failing
    # trap push.
    subprocess.run([str(script)], cwd=tmp_path, text=True, capture_output=True, env=env, check=True)


def test_partial_commit_on_timeout_emits_distinct_observability_event(tmp_path: Path) -> None:
    """Operators need to tell apart 'timeout produced work' vs 'timeout wasted
    a run' at a glance. The partial_commit_on_timeout event surfaces the former."""
    obs_dir = tmp_path / "obs"
    out = _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="1",
        timeout_mode="always",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
    )
    events_text = (obs_dir / "checkpoint_events.csv").read_text()
    assert "partial_commit_on_timeout" in events_text
    assert "partial progress committed" in out


def test_clean_tree_timeout_does_not_emit_partial_commit_event(tmp_path: Path) -> None:
    """If there was nothing to commit on timeout, don't spam the partial-commit
    event — it's meant to signal forward progress, not just that a timeout fired."""
    obs_dir = tmp_path / "obs"
    _run_loop(
        tmp_path,
        scenario="has_more_forever",
        git_dirty="0",
        timeout_mode="always",
        FIGMACLAW_SYNC_OBS_DIR=str(obs_dir),
    )
    events_text = (obs_dir / "checkpoint_events.csv").read_text()
    assert "partial_commit_on_timeout" not in events_text


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
