#!/usr/bin/env sh
# figmaclaw installer
# Usage: curl -fsSL https://raw.githubusercontent.com/aviadr1/figmaclaw/main/install.sh | sh
#
# For development setup, use: git clone + ./dev-setup.sh

set -e

GITHUB_REPO="https://github.com/aviadr1/figmaclaw"

echo "Installing figmaclaw from $GITHUB_REPO ..."

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: 'uv' is required but not found." >&2
    echo "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

uv tool install --force --reinstall --upgrade "git+$GITHUB_REPO@main"

echo ""
echo "figmaclaw installed successfully."
echo "Run 'figmaclaw --help' to get started."
echo "Run 'figmaclaw self skill' to see the agent usage guide."
