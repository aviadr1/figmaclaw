#!/usr/bin/env bash
# End-to-end installation test for figmaclaw
#
# Tests the full installation procedure in an isolated environment:
#   1. Install figmaclaw from git (simulates a new user)
#   2. Verify CLI works
#   3. Initialize a fresh consumer repo
#   4. Run doctor to validate setup
#   5. Track a Figma file and pull (requires FIGMA_API_KEY)
#
# Usage:
#   # Smoke test (no API key needed):
#   ./tests/test_install_e2e.sh
#
#   # Full test (requires FIGMA_API_KEY):
#   FIGMA_API_KEY=figd_... ./tests/test_install_e2e.sh --full
#
# Exit codes:
#   0 — all checks passed
#   1 — a check failed

set -euo pipefail

FULL_TEST=false
if [ "${1:-}" = "--full" ]; then
    FULL_TEST=true
fi

PASS=0
FAIL=0
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

check() {
    local label="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "  ✓ $label"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $label"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== figmaclaw installation test ==="
echo ""

# --- Phase 1: Install ---
echo "Phase 1: Install"

# Check uv is available
check "uv available" command -v uv

# Install figmaclaw to an isolated tool dir
INSTALL_DIR="$TMPDIR/tools"
export UV_TOOL_DIR="$INSTALL_DIR"
export UV_TOOL_BIN_DIR="$INSTALL_DIR/bin"
mkdir -p "$UV_TOOL_BIN_DIR"
export PATH="$UV_TOOL_BIN_DIR:$PATH"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
check "install from local git" uv tool install "$REPO_ROOT"

# --- Phase 2: Verify CLI ---
echo ""
echo "Phase 2: Verify CLI"

check "figmaclaw --version" figmaclaw --version
check "figmaclaw --help" figmaclaw --help
check "figmaclaw inspect --help" figmaclaw inspect --help
check "figmaclaw screenshots --help" figmaclaw screenshots --help
check "figmaclaw claude-run --help" figmaclaw claude-run --help
check "figmaclaw stream-format --help" figmaclaw stream-format --help
check "figmaclaw doctor --help" figmaclaw doctor --help
check "figmaclaw self skill --list" figmaclaw self skill --list
check "figmaclaw self skill figma-enrich-page" figmaclaw self skill figma-enrich-page

# --- Phase 3: Initialize consumer repo ---
echo ""
echo "Phase 3: Initialize consumer repo"

CONSUMER="$TMPDIR/test-consumer"
mkdir -p "$CONSUMER"
cd "$CONSUMER"
git init -q
git config user.name "test"
git config user.email "test@test.com"

check "figmaclaw init" figmaclaw --repo-dir "$CONSUMER" init
check "sync workflow exists" test -f "$CONSUMER/.github/workflows/figmaclaw-sync.yaml"
check "webhook workflow exists" test -f "$CONSUMER/.github/workflows/figmaclaw-webhook.yaml"

# --- Phase 4: Doctor ---
echo ""
echo "Phase 4: Doctor (no API key)"

# Doctor should run and report issues (exits 1 without API key, that's expected)
DOCTOR_OUTPUT=$(figmaclaw --repo-dir "$CONSUMER" doctor 2>&1 || true)
echo "$DOCTOR_OUTPUT" | grep -q "figmaclaw installed" && {
    echo "  ✓ doctor runs without crash"
    PASS=$((PASS + 1))
} || {
    echo "  ✗ doctor runs without crash"
    echo "$DOCTOR_OUTPUT"
    FAIL=$((FAIL + 1))
}
echo "$DOCTOR_OUTPUT" | grep -q "CI workflows installed" && {
    echo "  ✓ doctor detects installed workflows"
    PASS=$((PASS + 1))
} || {
    echo "  ✗ doctor detects installed workflows"
    FAIL=$((FAIL + 1))
}

# --- Phase 5: Full test (optional) ---
if [ "$FULL_TEST" = true ]; then
    echo ""
    echo "Phase 5: Full test (API integration)"

    if [ -z "${FIGMA_API_KEY:-}" ]; then
        echo "  ✗ FIGMA_API_KEY not set — skipping full test"
        FAIL=$((FAIL + 1))
    else
        # Use the Gigaverse mobile app file as a test target
        TEST_FILE_KEY="7az6PPiHUQumhxtV935xuD"

        check "doctor with API key" figmaclaw --repo-dir "$CONSUMER" doctor
        check "track file" figmaclaw --repo-dir "$CONSUMER" track "$TEST_FILE_KEY" --no-pull
        check "manifest created" test -f "$CONSUMER/.figma-sync/manifest.json"
        check "pull (max 1 page)" figmaclaw --repo-dir "$CONSUMER" pull --max-pages 1

        # Find a generated .md and inspect it
        FIRST_MD=$(find "$CONSUMER/figma" -name "*.md" -type f | head -1)
        if [ -n "$FIRST_MD" ]; then
            check "inspect generated page" figmaclaw --repo-dir "$CONSUMER" inspect "$FIRST_MD" --json
        else
            echo "  ✗ no .md files generated after pull"
            FAIL=$((FAIL + 1))
        fi
    fi
else
    echo ""
    echo "Phase 5: Skipped (run with --full and FIGMA_API_KEY to test API integration)"
fi

# --- Summary ---
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
