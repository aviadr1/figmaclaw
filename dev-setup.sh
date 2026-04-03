#!/usr/bin/env bash
# Development setup for figmaclaw contributors
# Usage: git clone https://github.com/aviadr1/figmaclaw && cd figmaclaw && ./dev-setup.sh
set -euo pipefail

echo "Setting up figmaclaw development environment..."
uv sync
pre-commit install
echo ""
echo "Done. Verify with:"
echo "  uv run figmaclaw --help"
echo "  uv run pytest -m 'not smoke'"
