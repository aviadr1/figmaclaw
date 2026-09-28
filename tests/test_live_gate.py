"""Live smoke tests skip, never fail, when Figma has no credential it accepts.

Figma personal access tokens expire after at most 90 days, so an expired
token is the steady state between rotations, not a regression. Any other
failure must still reach the test and fail it.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from _pytest.outcomes import Skipped

from figmaclaw.figma_mcp import FigmaMcpClient, FigmaMcpError
from tests.smoke import live_gate
from tests.smoke.live_gate import (
    require_accepted_mcp_token,
    require_live_credential,
    require_valid_figma_pat,
    skipped_live_reasons,
)

_ME_URL = "https://api.figma.com/v1/me"
_MCP_URL = "https://mcp.figma.com/mcp"


@pytest.fixture(autouse=True)
def _fresh_gate() -> Iterator[None]:
    live_gate.reset_for_tests()
    yield
    live_gate.reset_for_tests()


def test_require_live_credential_returns_value_when_present() -> None:
    assert require_live_credential("token-123", name="FIGMA_API_KEY", hint="set it") == "token-123"


def test_missing_credential_skips_even_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(Skipped, match="FIGMA_API_KEY not set"):
        require_live_credential("", name="FIGMA_API_KEY", hint="set it")
    assert skipped_live_reasons() == ["FIGMA_API_KEY not set. set it"]


@respx.mock
def test_accepted_pat_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIGMA_API_KEY", "figd_valid")
    me = respx.get(_ME_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))

    assert require_valid_figma_pat(hint="set it") == "figd_valid"
    assert me.calls.last.request.headers["X-Figma-Token"] == "figd_valid"


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
def test_rejected_pat_skips_and_names_the_90_day_expiry(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("FIGMA_API_KEY", "figd_expired")
    respx.get(_ME_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(Skipped, match=rf"HTTP {status}.*90 days"):
        require_valid_figma_pat(hint="set it")


@respx.mock
def test_pat_is_probed_once_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIGMA_API_KEY", "figd_expired")
    me = respx.get(_ME_URL).mock(return_value=httpx.Response(403))

    for _ in range(3):
        with pytest.raises(Skipped):
            require_valid_figma_pat(hint="set it")
    assert me.call_count == 1
    assert len(skipped_live_reasons()) == 1


@respx.mock
def test_figma_outage_is_not_mistaken_for_an_expired_pat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: only an auth rejection skips; a 5xx lets the tests run and fail."""
    monkeypatch.setenv("FIGMA_API_KEY", "figd_valid")
    respx.get(_ME_URL).mock(return_value=httpx.Response(503))

    assert require_valid_figma_pat(hint="set it") == "figd_valid"
    assert skipped_live_reasons() == []


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
def test_rejected_mcp_token_skips(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setenv("FIGMA_MCP_TOKEN", "expired-oauth")
    respx.post(_MCP_URL).mock(return_value=httpx.Response(status, text="Unauthorized"))

    with pytest.raises(Skipped, match=rf"FIGMA_MCP_TOKEN.*HTTP {status}"):
        require_accepted_mcp_token()


@respx.mock
def test_mcp_server_error_is_not_mistaken_for_a_rejected_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: only an auth rejection skips MCP smoke tests."""
    monkeypatch.setenv("FIGMA_MCP_TOKEN", "valid-oauth")
    respx.post(_MCP_URL).mock(return_value=httpx.Response(500, text="boom"))

    require_accepted_mcp_token()
    assert skipped_live_reasons() == []


@respx.mock
async def test_mcp_http_errors_carry_their_status() -> None:
    respx.post(_MCP_URL).mock(return_value=httpx.Response(401, text="Unauthorized"))

    with pytest.raises(FigmaMcpError) as excinfo:
        await FigmaMcpClient("expired-oauth").list_tools()
    assert excinfo.value.http_status == 401
