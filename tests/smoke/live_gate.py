"""Helpers to enforce credentialed live smoke execution."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.env_utils import load_repo_dotenv

load_repo_dotenv(Path(__file__).resolve().parents[2])


def require_live_credential(value: str, *, name: str, hint: str) -> str:
    """Return credential value, skip when missing locally, fail when gated.

    CI's dedicated smoke jobs set ``FIGMACLAW_REQUIRE_LIVE_SMOKE=1`` and a
    missing credential there is a configuration error — fail loudly so the
    job goes red. Everywhere else (developer machines, the main CI test
    job which excludes ``-m smoke_*``), absence is the expected steady
    state — skip with the hint instead of erroring out.
    """
    if value:
        return value

    message = f"{name} not set. {hint}"
    if os.environ.get("FIGMACLAW_REQUIRE_LIVE_SMOKE", "").strip() == "1":
        pytest.fail(message)
    pytest.skip(message)
