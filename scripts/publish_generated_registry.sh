#!/usr/bin/env bash
set -euo pipefail

# Publish deterministic generated registry commits from a GitHub runner.
#
# Canon WF-1/WF-4/WF-6: generated registry snapshots are replayable, but not
# every rejected push means the registry path changed. If the remote advanced
# only on unrelated paths, rebase the local generated commits onto the latest
# target ref. If the protected registry path changed, reset and replay the
# generator from the latest target ref.

: "${TARGET_REF:?TARGET_REF is required}"
: "${PUBLISH_PROTECTED_PATH_RE:?PUBLISH_PROTECTED_PATH_RE is required}"
: "${REPLAY_COMMAND:?REPLAY_COMMAND is required}"

MAX_PUBLISH_ATTEMPTS="${MAX_PUBLISH_ATTEMPTS:-6}"

remote_touched_protected_path() {
  local paths
  if ! paths="$(git diff --name-only "HEAD...origin/${TARGET_REF}" 2>/dev/null)"; then
    # Shallow checkouts can lack a merge-base. Fall back to a conservative
    # snapshot diff; it may replay more often, but it will not rebase a stale
    # protected registry snapshot over remote protected-registry movement.
    paths="$(git diff --name-only HEAD "origin/${TARGET_REF}" || true)"
  fi
  printf "%s\n" "$paths" | grep -Eq "$PUBLISH_PROTECTED_PATH_RE"
}

replay_generated_registry() {
  echo "Replaying generated registry refresh on latest ${TARGET_REF}"
  git fetch origin "$TARGET_REF"
  # This is safe only because this GitHub runner has no human edits, this job
  # writes only deterministic registry artifacts, and the next command
  # immediately recomputes those artifacts from Figma. Do not copy this reset
  # pattern to LLM/human-authored body workflows.
  git reset --hard "origin/${TARGET_REF}"
  bash -c "$REPLAY_COMMAND"
}

for attempt in $(seq 1 "$MAX_PUBLISH_ATTEMPTS"); do
  echo "Publish attempt ${attempt}/${MAX_PUBLISH_ATTEMPTS}"
  git fetch origin "$TARGET_REF"

  LOCAL_COMMITS="$(git rev-list --count "origin/${TARGET_REF}..HEAD")"
  if [ "$LOCAL_COMMITS" = "0" ]; then
    echo "No local generated registry commits to publish."
    exit 0
  fi

  set +e
  git push
  PUSH_STATUS=$?
  set -e
  if [ "$PUSH_STATUS" -eq 0 ]; then
    exit 0
  fi

  echo "Push rejected; inspecting remote movement on ${TARGET_REF}"
  git fetch origin "$TARGET_REF"
  if remote_touched_protected_path; then
    echo "Remote touched protected registry path(s); reset/replay is required."
    replay_generated_registry
  else
    echo "Remote did not touch protected registry path(s); rebasing generated commits."
    if ! git rebase "origin/${TARGET_REF}"; then
      echo "Rebase failed; falling back to reset/replay."
      git rebase --abort || true
      replay_generated_registry
    fi
  fi
done

echo "::error::Could not publish generated registry after ${MAX_PUBLISH_ATTEMPTS} attempts."
exit 1
