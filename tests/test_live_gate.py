from __future__ import annotations

import pytest
from _pytest.outcomes import Failed, Skipped

from tests.smoke.live_gate import require_live_credential


def test_require_live_credential_returns_value_when_present() -> None:
    value = require_live_credential("token-123", name="FIGMA_API_KEY", hint="set it")
    assert value == "token-123"


def test_require_live_credential_skips_when_missing_and_not_gated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Local/dev path: missing credentials should skip the smoke test, not error.

    The main CI test job already excludes ``-m smoke_*`` and gets here only
    if a contributor runs the full suite locally; an "ERROR FIGMA_MCP_TOKEN
    not set" out of nowhere is bad DX.
    """
    monkeypatch.delenv("FIGMACLAW_REQUIRE_LIVE_SMOKE", raising=False)
    with pytest.raises(Skipped):
        require_live_credential("", name="FIGMA_API_KEY", hint="set it")


def test_require_live_credential_fails_when_gated_and_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Gated path (CI's dedicated smoke job): missing creds is a config error
    — fail loudly so the job goes red instead of silently skipping."""
    monkeypatch.setenv("FIGMACLAW_REQUIRE_LIVE_SMOKE", "1")
    with pytest.raises(Failed):
        require_live_credential("", name="FIGMA_API_KEY", hint="set it")
