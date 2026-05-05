#!/usr/bin/env bash
set -euo pipefail

# Reusable checkpointed pull loop for sync.yml.
# Kept in one script so behavior is testable and shared across callers.

INPUT_FORCE="${INPUT_FORCE:-false}"
MAX_BATCHES="${MAX_BATCHES:-180}"
MAX_IDLE_HAS_MORE_BATCHES="${MAX_IDLE_HAS_MORE_BATCHES:-3}"
BATCH_TIMEOUT_SECONDS="${BATCH_TIMEOUT_SECONDS:-420}"
MAX_PAGES_PER_BATCH="${MAX_PAGES_PER_BATCH:-5}"
FIGMACLAW_OUT_PATH="${FIGMACLAW_OUT_PATH:-/tmp/figmaclaw-out.txt}"
FIGMA_TEAM_ID="${FIGMA_TEAM_ID:-}"
SINCE="${SINCE:-3m}"
TARGET_REF="${TARGET_REF:-main}"
FIGMACLAW_SYNC_OBS_DIR="${FIGMACLAW_SYNC_OBS_DIR:-}"
# Delegate commit/push to figmaclaw itself at page granularity. Makes individual
# pages durable in origin as soon as they're written, so a batch-level SIGKILL
# no longer discards up-to-N-pages of work. The shell's commit_if_changed still
# runs afterward as a safety net for manifest tail updates.
AUTO_COMMIT_ENABLED="${AUTO_COMMIT_ENABLED:-true}"
PUSH_EVERY="${PUSH_EVERY:-1}"

declare -a PULL_ARGS
CURRENT_MAX_PAGES_PER_BATCH="$MAX_PAGES_PER_BATCH"
SCRIPT_START_EPOCH="$(date +%s)"
OBS_EVENTS_FILE=""
OBS_SUMMARY_FILE=""
BATCHES_STARTED=0
TOTAL_TIMEOUTS=0
TOTAL_BACKOFFS=0
TOTAL_COMMITS=0
FINAL_REASON="unknown"

PULL_STATUS=0
PULL_DURATION_S=0
GIT_PULL_S=0
GIT_ADD_S=0
GIT_DIFF_S=0
GIT_COMMIT_S=0
GIT_PUSH_S=0
HAS_MORE="false"

sanitize_obs_field() {
  echo "$1" | tr '\n\r,' '   '
}

emit_obs() {
  local event="$1"
  local reason="${2:-}"
  local ts_utc elapsed_s safe_reason
  ts_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  elapsed_s="$(( $(date +%s) - SCRIPT_START_EPOCH ))"
  safe_reason="$(sanitize_obs_field "$reason")"

  echo "SYNC_OBS event=${event} batch=${BATCH:-0} elapsed_s=${elapsed_s} max_pages=${CURRENT_MAX_PAGES_PER_BATCH} pull_status=${PULL_STATUS} committed=${committed:-na} has_more=${HAS_MORE} idle_has_more=${idle_has_more:-0} reason=\"${safe_reason}\""

  if [ -z "$OBS_EVENTS_FILE" ]; then
    return
  fi

  cat >> "$OBS_EVENTS_FILE" <<EOF
${ts_utc},${elapsed_s},${BATCH:-0},${event},${INPUT_FORCE},${CURRENT_MAX_PAGES_PER_BATCH},${PULL_STATUS},${PULL_DURATION_S},${GIT_PULL_S},${GIT_ADD_S},${GIT_DIFF_S},${GIT_COMMIT_S},${GIT_PUSH_S},${committed:-na},${HAS_MORE},${idle_has_more:-0},${safe_reason}
EOF
}

init_observability() {
  if [ -z "$FIGMACLAW_SYNC_OBS_DIR" ]; then
    return
  fi
  mkdir -p "$FIGMACLAW_SYNC_OBS_DIR"
  OBS_EVENTS_FILE="${FIGMACLAW_SYNC_OBS_DIR}/checkpoint_events.csv"
  OBS_SUMMARY_FILE="${FIGMACLAW_SYNC_OBS_DIR}/checkpoint_summary.txt"
  cat > "$OBS_EVENTS_FILE" <<EOF
ts_utc,elapsed_s,batch,event,input_force,max_pages,pull_status,pull_duration_s,git_pull_s,git_add_s,git_diff_s,git_commit_s,git_push_s,committed,has_more,idle_has_more,reason
EOF
}

set_pull_args() {
  if [ "$INPUT_FORCE" = "true" ]; then
    PULL_ARGS=(--force)
  else
    PULL_ARGS=(--max-pages "$CURRENT_MAX_PAGES_PER_BATCH")
  fi
  if [ -n "$FIGMA_TEAM_ID" ]; then
    PULL_ARGS+=(--team-id "$FIGMA_TEAM_ID" --since "$SINCE")
  fi
  # Page-level commit+push keeps partial progress in origin even if the Python
  # process is SIGKILL'd mid-batch — the next CI run's fresh checkout picks it up.
  if [ "$AUTO_COMMIT_ENABLED" = "true" ]; then
    PULL_ARGS+=(--auto-commit --push-every "$PUSH_EVERY")
  fi
}

run_pull_batch() {
  local t0 t1
  t0="$(date +%s)"
  set +e
  timeout "$BATCH_TIMEOUT_SECONDS" figmaclaw pull "${PULL_ARGS[@]}" | tee "$FIGMACLAW_OUT_PATH"
  PULL_STATUS=${PIPESTATUS[0]}
  set -e
  t1="$(date +%s)"
  PULL_DURATION_S="$((t1 - t0))"
}

commit_if_changed() {
  local msg_override="${1:-}"
  local t0 t1
  GIT_PULL_S=0
  GIT_ADD_S=0
  GIT_DIFF_S=0
  GIT_COMMIT_S=0
  GIT_PUSH_S=0

  t0="$(date +%s)"
  git pull --no-rebase --ff-only origin "$TARGET_REF" >&2
  t1="$(date +%s)"
  GIT_PULL_S="$((t1 - t0))"

  t0="$(date +%s)"
  git add figma/ .figma-sync/ >&2
  t1="$(date +%s)"
  GIT_ADD_S="$((t1 - t0))"

  t0="$(date +%s)"
  if git diff --cached --quiet >&2; then
    t1="$(date +%s)"
    GIT_DIFF_S="$((t1 - t0))"
    echo "false"
    return
  fi
  t1="$(date +%s)"
  GIT_DIFF_S="$((t1 - t0))"

  if [ -n "$msg_override" ]; then
    COMMIT_MSG="$msg_override"
  else
    COMMIT_MSG="$(grep '^COMMIT_MSG:' "$FIGMACLAW_OUT_PATH" | head -1 | sed 's/^COMMIT_MSG://' | tr -d '\n\r')"
    COMMIT_MSG="${COMMIT_MSG:-sync: figmaclaw — checkpoint batch $BATCH}"
  fi

  t0="$(date +%s)"
  git commit -m "${COMMIT_MSG}" >&2
  t1="$(date +%s)"
  GIT_COMMIT_S="$((t1 - t0))"

  t0="$(date +%s)"
  git push >&2
  t1="$(date +%s)"
  GIT_PUSH_S="$((t1 - t0))"
  echo "true"
}

init_observability

# Final-push safety net. With --auto-commit and any PUSH_EVERY value, a SIGKILL
# between figmaclaw's `git commit` and `git push` can leave local commits that
# never made it to origin. The next CI run does `actions/checkout@v6` (fresh
# tree) and throws those commits away, which is exactly the data-loss shape
# this whole PR exists to fix. The trap fires on normal exit, error exit, and
# signals — `git push` with nothing to push is a no-op, so it's always safe.
final_push_flush() {
  git push >&2 || true
}
trap final_push_flush EXIT

emit_obs "loop_start" "checkpoint loop started"

idle_has_more=0
BATCH=0
committed="na"
while true; do
  BATCH=$((BATCH + 1))
  if [ "$BATCH" -gt "$MAX_BATCHES" ]; then
    echo "Reached MAX_BATCHES=$MAX_BATCHES; stopping checkpoint loop early."
    FINAL_REASON="max_batches"
    emit_obs "loop_break" "Reached MAX_BATCHES"
    break
  fi

  BATCHES_STARTED=$((BATCHES_STARTED + 1))
  HAS_MORE="false"
  committed="na"
  echo "--- batch $BATCH ---"
  emit_obs "batch_start" "starting batch"

  set_pull_args
  run_pull_batch
  pull_status="$PULL_STATUS"

  if [ "$pull_status" -eq 124 ]; then
    TOTAL_TIMEOUTS=$((TOTAL_TIMEOUTS + 1))
    # Persist partial progress before retrying/stopping. Two sources of work to save:
    #   1. Local page commits from --auto-commit that weren't pushed yet (possible
    #      when PUSH_EVERY > 1). Push them explicitly; best-effort, so failures
    #      don't break the loop.
    #   2. Any remaining dirty working-tree state (manifest tail updates, component
    #      writes not attached to a page commit). commit_if_changed handles this.
    # Without these, the next CI run's fresh `actions/checkout@v6` throws all
    # mid-batch work away — causing the loop to re-do the same schema upgrades
    # forever without ever landing a commit upstream.
    git push >&2 || true
    committed="$(commit_if_changed "sync: figmaclaw — partial progress (batch $BATCH timeout)")"
    if [ "$committed" = "true" ]; then
      TOTAL_COMMITS=$((TOTAL_COMMITS + 1))
      # Distinct event + log line so operators can tell at a glance whether a
      # timed-out batch made forward progress or was completely wasted.
      echo "figmaclaw pull timed out but partial progress committed (batch $BATCH)."
      emit_obs "partial_commit_on_timeout" "timeout commit saved work"
    else
      echo "figmaclaw pull timed out after ${BATCH_TIMEOUT_SECONDS}s with no dirty progress; stopping checkpoint loop early."
      FINAL_REASON="timeout_no_progress_stop"
      emit_obs "batch_timeout_stop" "timeout without dirty progress"
      emit_obs "batch_end" "timeout no progress"
      break
    fi
    if [ "$INPUT_FORCE" != "true" ] && [ "$CURRENT_MAX_PAGES_PER_BATCH" -gt 1 ]; then
      CURRENT_MAX_PAGES_PER_BATCH=$((CURRENT_MAX_PAGES_PER_BATCH / 2))
      if [ "$CURRENT_MAX_PAGES_PER_BATCH" -lt 1 ]; then
        CURRENT_MAX_PAGES_PER_BATCH=1
      fi
      TOTAL_BACKOFFS=$((TOTAL_BACKOFFS + 1))
      echo "figmaclaw pull timed out after ${BATCH_TIMEOUT_SECONDS}s; retrying with --max-pages ${CURRENT_MAX_PAGES_PER_BATCH}."
      emit_obs "batch_timeout_backoff" "timeout with retry"
      continue
    fi
    echo "figmaclaw pull timed out after ${BATCH_TIMEOUT_SECONDS}s; stopping checkpoint loop early."
    FINAL_REASON="timeout_stop"
    emit_obs "batch_timeout_stop" "timeout without retry"
    emit_obs "batch_end" "timeout stop"
    break
  fi

  # After a successful backoff retry, restore the default batch size for throughput.
  CURRENT_MAX_PAGES_PER_BATCH="$MAX_PAGES_PER_BATCH"

  committed="$(commit_if_changed)"
  if [ "$committed" = "true" ]; then
    TOTAL_COMMITS=$((TOTAL_COMMITS + 1))
  fi

  if grep -q '^HAS_MORE:true' "$FIGMACLAW_OUT_PATH"; then
    HAS_MORE="true"
    if [ "$committed" = false ]; then
      idle_has_more=$((idle_has_more + 1))
      echo "HAS_MORE:true with no commit (idle_has_more=$idle_has_more/$MAX_IDLE_HAS_MORE_BATCHES)"
      if [ "$idle_has_more" -ge "$MAX_IDLE_HAS_MORE_BATCHES" ]; then
        echo "Stopping loop after repeated HAS_MORE:true without progress."
        FINAL_REASON="idle_has_more_limit"
        emit_obs "batch_end" "idle has_more limit reached"
        break
      fi
    else
      idle_has_more=0
    fi
  else
    FINAL_REASON="has_more_false"
    emit_obs "batch_end" "HAS_MORE false"
    break
  fi

  emit_obs "batch_end" "HAS_MORE true, continuing"

  if [ "$INPUT_FORCE" = "true" ]; then
    FINAL_REASON="force_single_batch"
    emit_obs "loop_break" "force mode single batch"
    break
  fi
done

emit_obs "loop_end" "$FINAL_REASON"

if [ -n "$OBS_SUMMARY_FILE" ]; then
  total_elapsed_s="$(( $(date +%s) - SCRIPT_START_EPOCH ))"
  cat > "$OBS_SUMMARY_FILE" <<EOF
total_elapsed_s=${total_elapsed_s}
batches_started=${BATCHES_STARTED}
total_commits=${TOTAL_COMMITS}
total_timeouts=${TOTAL_TIMEOUTS}
total_backoffs=${TOTAL_BACKOFFS}
final_reason=${FINAL_REASON}
max_batches=${MAX_BATCHES}
max_pages_per_batch=${MAX_PAGES_PER_BATCH}
batch_timeout_seconds=${BATCH_TIMEOUT_SECONDS}
input_force=${INPUT_FORCE}
EOF
  echo "SYNC_OBS summary_file=${OBS_SUMMARY_FILE}"
  echo "SYNC_OBS events_file=${OBS_EVENTS_FILE}"
fi
