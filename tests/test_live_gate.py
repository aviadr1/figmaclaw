from __future__ import annotations

import pytest
from _pytest.outcomes import Failed

from tests.smoke.live_gate import require_live_credential


def test_require_live_credential_returns_value_when_present() -> None:
    value = require_live_credential("token-123", name="FIGMA_API_KEY", hint="set it")
    assert value == "token-123"


def test_require_live_credential_fails_when_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("FIGMACLAW_REQUIRE_LIVE_SMOKE", raising=False)
    with pytest.raises(Failed):
        require_live_credential("", name="FIGMA_API_KEY", hint="set it")
