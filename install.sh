#!/usr/bin/env bash
set -euo pipefail

echo "Installing figmaclaw..."
uv sync
pre-commit install
echo ""
echo "Done. Verify with:"
echo "  uv run figmaclaw --help"
echo "  uv run pytest -m 'not smoke'"
