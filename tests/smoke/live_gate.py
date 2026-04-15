"""Helpers to enforce credentialed live smoke execution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.env_utils import load_repo_dotenv

load_repo_dotenv(Path(__file__).resolve().parents[2])


def require_live_credential(value: str, *, name: str, hint: str) -> str:
    """Return credential value or fail loudly when missing."""
    if value:
        return value

    message = f"{name} not set. {hint}"
    if os.environ.get("FIGMACLAW_REQUIRE_LIVE_SMOKE", "").strip() == "1":
        pytest.fail(message)
    pytest.fail(message)
