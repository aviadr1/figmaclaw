#!/usr/bin/env bash
set -euo pipefail

# Reusable checkpointed pull loop for sync.yml.
# Kept in one script so behavior is testable and shared across callers.

INPUT_FORCE="${INPUT_FORCE:-false}"
MAX_BATCHES="${MAX_BATCHES:-180}"
MAX_IDLE_HAS_MORE_BATCHES="${MAX_IDLE_HAS_MORE_BATCHES:-3}"
BATCH_TIMEOUT_SECONDS="${BATCH_TIMEOUT_SECONDS:-900}"
MAX_PAGES_PER_BATCH="${MAX_PAGES_PER_BATCH:-5}"
FIGMACLAW_OUT_PATH="${FIGMACLAW_OUT_PATH:-/tmp/figmaclaw-out.txt}"

declare -a PULL_ARGS
CURRENT_MAX_PAGES_PER_BATCH="$MAX_PAGES_PER_BATCH"

set_pull_args() {
  if [ "$INPUT_FORCE" = "true" ]; then
    PULL_ARGS=(--force)
  else
    PULL_ARGS=(--max-pages "$CURRENT_MAX_PAGES_PER_BATCH")
  fi
}

run_pull_batch() {
  set +e
  timeout "$BATCH_TIMEOUT_SECONDS" figmaclaw pull "${PULL_ARGS[@]}" | tee "$FIGMACLAW_OUT_PATH"
  PULL_STATUS=${PIPESTATUS[0]}
  set -e
}

commit_if_changed() {
  git pull --no-rebase --ff-only origin main
  git add figma/ .figma-sync/
  if git diff --cached --quiet; then
    echo "false"
    return
  fi

  COMMIT_MSG="$(grep '^COMMIT_MSG:' "$FIGMACLAW_OUT_PATH" | head -1 | sed 's/^COMMIT_MSG://' | tr -d '\n\r')"
  COMMIT_MSG="${COMMIT_MSG:-sync: figmaclaw — checkpoint batch $BATCH}"
  git commit -m "${COMMIT_MSG}"
  git push
  echo "true"
}

idle_has_more=0
PULL_STATUS=0
BATCH=0
while true; do
  BATCH=$((BATCH + 1))
  if [ "$BATCH" -gt "$MAX_BATCHES" ]; then
    echo "Reached MAX_BATCHES=$MAX_BATCHES; stopping checkpoint loop early."
    break
  fi

  echo "--- batch $BATCH ---"

  set_pull_args
  run_pull_batch
  pull_status="$PULL_STATUS"

  if [ "$pull_status" -eq 124 ]; then
    if [ "$INPUT_FORCE" != "true" ] && [ "$CURRENT_MAX_PAGES_PER_BATCH" -gt 1 ]; then
      CURRENT_MAX_PAGES_PER_BATCH=$((CURRENT_MAX_PAGES_PER_BATCH / 2))
      if [ "$CURRENT_MAX_PAGES_PER_BATCH" -lt 1 ]; then
        CURRENT_MAX_PAGES_PER_BATCH=1
      fi
      echo "figmaclaw pull timed out after ${BATCH_TIMEOUT_SECONDS}s; retrying with --max-pages ${CURRENT_MAX_PAGES_PER_BATCH}."
      continue
    fi
    echo "figmaclaw pull timed out after ${BATCH_TIMEOUT_SECONDS}s; stopping checkpoint loop early."
    break
  fi

  # After a successful backoff retry, restore the default batch size for throughput.
  CURRENT_MAX_PAGES_PER_BATCH="$MAX_PAGES_PER_BATCH"

  committed="$(commit_if_changed)"

  if grep -q '^HAS_MORE:true' "$FIGMACLAW_OUT_PATH"; then
    if [ "$committed" = false ]; then
      idle_has_more=$((idle_has_more + 1))
      echo "HAS_MORE:true with no commit (idle_has_more=$idle_has_more/$MAX_IDLE_HAS_MORE_BATCHES)"
      if [ "$idle_has_more" -ge "$MAX_IDLE_HAS_MORE_BATCHES" ]; then
        echo "Stopping loop after repeated HAS_MORE:true without progress."
        break
      fi
    else
      idle_has_more=0
    fi
  else
    break
  fi

  [ "$INPUT_FORCE" = "true" ] && break
done
