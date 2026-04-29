#!/usr/bin/env bash
set -euo pipefail

REPO="gigaverse-app/linear-git"
WORKFLOW="figmaclaw-sync.yaml"
REF="main"
RUN_ID=""
TIMEOUT_SECONDS=5400

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Dispatch and soak-check a reusable figmaclaw workflow run, then scan full logs
for known warning/error patterns.

Options:
  --repo <owner/repo>       Target consumer repository (default: ${REPO})
  --workflow <file|name>    Workflow file/name (default: ${WORKFLOW})
  --ref <branch>            Branch/ref to dispatch (default: ${REF})
  --run-id <id>             Existing run id (skip dispatch)
  --timeout-seconds <n>     Watch timeout in seconds (default: ${TIMEOUT_SECONDS})
  -h, --help                Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="$2"
      shift 2
      ;;
    --workflow)
      WORKFLOW="$2"
      shift 2
      ;;
    --ref)
      REF="$2"
      shift 2
      ;;
    --run-id)
      RUN_ID="$2"
      shift 2
      ;;
    --timeout-seconds)
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 2
  fi
}

require_cmd gh

if [[ -z "${RUN_ID}" ]]; then
  echo "Dispatching workflow '${WORKFLOW}' on ${REPO}@${REF}" >&2
  gh workflow run "${WORKFLOW}" -R "${REPO}" --ref "${REF}"
  sleep 5
  RUN_ID="$(gh run list -R "${REPO}" --workflow "${WORKFLOW}" --branch "${REF}" --limit 1 --json databaseId --jq '.[0].databaseId')"
fi

if [[ -z "${RUN_ID}" || "${RUN_ID}" == "null" ]]; then
  echo "Could not resolve run id" >&2
  exit 2
fi

echo "Watching run ${RUN_ID} in ${REPO}" >&2
gh run watch "${RUN_ID}" -R "${REPO}" --exit-status --interval 10

log_file="$(mktemp)"
trap 'rm -f "${log_file}"' EXIT

gh run view "${RUN_ID}" -R "${REPO}" --log > "${log_file}"

declare -a DENY_PATTERNS=(
  "PHANTOM SELECTION"
  "unsupported enrichment log schema/header"
  "The following paths are ignored by one of your \\\.gitignore files"
  "NO-PROGRESS"
  "STUCK:"
)

failed=0
for pattern in "${DENY_PATTERNS[@]}"; do
  if grep -nE "${pattern}" "${log_file}" >/tmp/soak_match.txt; then
    echo "[soak] FOUND pattern: ${pattern}" >&2
    cat /tmp/soak_match.txt >&2
    failed=1
  fi
done
rm -f /tmp/soak_match.txt

if [[ ${failed} -ne 0 ]]; then
  echo "[soak] FAIL: warning/error patterns detected in run logs" >&2
  echo "Run URL: https://github.com/${REPO}/actions/runs/${RUN_ID}" >&2
  exit 2
fi

echo "[soak] PASS: no known warning/error patterns detected" >&2
echo "Run URL: https://github.com/${REPO}/actions/runs/${RUN_ID}" >&2
