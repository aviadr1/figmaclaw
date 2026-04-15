"""Minimal .env loader for tests (no external dependency)."""

from __future__ import annotations

import os
from pathlib import Path


def load_repo_dotenv(repo_root: Path) -> None:
    """Load KEY=VALUE pairs from repo .env into os.environ if not already set."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value
