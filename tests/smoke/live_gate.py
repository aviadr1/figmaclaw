"""Gate live smoke tests on a Figma credential that Figma currently accepts.

Figma personal access tokens expire after at most 90 days, so a missing or
expired token is the normal state between rotations, not a regression. Live
smoke tests therefore skip, everywhere including CI, when no accepted
credential exists. The skip reasons are reported as GitHub warnings by
``tests/conftest.py`` so a skipped smoke job still shows why. Every other
failure, including a Figma outage, reaches the test and fails it.
"""

from __future__ import annotations

import asyncio
import functools
import os
from pathlib import Path
from typing import NoReturn

import httpx
import pytest

from figmaclaw.figma_mcp import FigmaMcpClient, FigmaMcpError
from tests.env_utils import load_repo_dotenv

load_repo_dotenv(Path(__file__).resolve().parents[2])

_AUTH_REJECTED = frozenset({401, 403})
_ME_URL = "https://api.figma.com/v1/me"
_skip_reasons: dict[str, None] = {}


def skipped_live_reasons() -> list[str]:
    """Distinct reasons live smoke tests were skipped in this session."""
    return list(_skip_reasons)


def reset_for_tests() -> None:
    _skip_reasons.clear()
    _figma_pat_status.cache_clear()
    _mcp_rejected_status.cache_clear()


def _skip_live(reason: str) -> NoReturn:
    _skip_reasons[reason] = None
    pytest.skip(reason)


def require_live_credential(value: str, *, name: str, hint: str) -> str:
    """Return the credential, or skip the test when it isn't configured."""
    if value:
        return value
    _skip_live(f"{name} not set. {hint}")


@functools.cache
def _figma_pat_status(api_key: str) -> int:
    """Probe once per session: the cheapest authenticated REST call."""
    return httpx.get(_ME_URL, headers={"X-Figma-Token": api_key}, timeout=30).status_code


def require_valid_figma_pat(*, hint: str) -> str:
    """Return FIGMA_API_KEY, or skip when it is missing or Figma rejects it."""
    api_key = require_live_credential(
        os.environ.get("FIGMA_API_KEY", ""), name="FIGMA_API_KEY", hint=hint
    )
    status = _figma_pat_status(api_key)
    if status in _AUTH_REJECTED:
        _skip_live(
            f"Figma rejected FIGMA_API_KEY (HTTP {status}). Personal access tokens "
            "expire after at most 90 days; generate a new one to run live smoke tests."
        )
    return api_key


@functools.cache
def _mcp_rejected_status() -> int | None:
    """Probe once per session with tools/list; None unless auth was rejected."""
    try:
        asyncio.run(FigmaMcpClient.auto().list_tools())
    except FigmaMcpError as exc:
        if exc.http_status in _AUTH_REJECTED:
            return exc.http_status
    return None


def require_accepted_mcp_token() -> None:
    """Skip when Figma's MCP server rejects the configured token."""
    status = _mcp_rejected_status()
    if status is not None:
        _skip_live(
            f"Figma's MCP server rejected FIGMA_MCP_TOKEN (HTTP {status}); it has "
            "expired or been revoked. Re-authenticate to run live MCP smoke tests."
        )
