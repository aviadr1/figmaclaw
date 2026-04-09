"""Tests for figma_mcp.py — FigmaMcpClient.

INVARIANTS:
- from_env() reads token from FIGMA_MCP_TOKEN; raises FigmaMcpError when unset
- from_claude_credentials() reads token from ~/.claude/.credentials.json;
  raises FigmaMcpError when missing or malformed
- auto() prefers env var over credentials file
- use_figma() sends exactly three POST requests to mcp.figma.com/mcp:
    1. initialize (no session header)
    2. notifications/initialized (with optional session header, no id)
    3. tools/call use_figma (with optional session header)
- use_figma() uses Authorization: Bearer, not X-Figma-Token
- use_figma() raises FigmaMcpError on HTTP >= 400
- use_figma() raises FigmaMcpError on JSON-RPC error responses
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from figmaclaw.figma_mcp import FigmaMcpClient, FigmaMcpError

FILE_KEY = "ABC123fileKey"
SESSION_ID = "sess-abc-123"
_MCP_URL = "https://mcp.figma.com/mcp"
_TOKEN = "test-oauth-token"


def _mock_three_step(
    init_resp: httpx.Response | None = None,
    notify_resp: httpx.Response | None = None,
    call_resp: httpx.Response | None = None,
    capture: list[httpx.Request] | None = None,
) -> respx.MockRouter:
    """Register a single respx route that returns three responses in order.

    Defaults to the normal happy-path responses when not overridden.
    Pass a list to *capture* to collect all requests made during the test.
    """
    responses: deque[httpx.Response] = deque([
        init_resp or httpx.Response(
            200,
            json=_init_response(),
            headers={"Mcp-Session-Id": SESSION_ID},
        ),
        notify_resp or httpx.Response(202),
        call_resp or httpx.Response(200, json=_tools_call_response()),
    ])

    def _side_effect(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return responses.popleft()

    router = respx.mock()
    router.post(_MCP_URL).mock(side_effect=_side_effect)
    return router


def _init_response() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "figma-mcp", "version": "1.0"},
        },
    }


def _tools_call_response() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [{"type": "text", "text": "hello from Figma"}],
            "isError": False,
        },
    }


class TestFromEnv:
    def test_reads_token_from_env(self) -> None:
        """INVARIANT: from_env() returns a client with the env var token."""
        with patch.dict(os.environ, {"FIGMA_MCP_TOKEN": "my-token"}):
            client = FigmaMcpClient.from_env()
        assert client._token == "my-token"

    def test_raises_when_env_var_missing(self) -> None:
        """INVARIANT: from_env() raises FigmaMcpError when env var is absent."""
        env = {k: v for k, v in os.environ.items() if k != "FIGMA_MCP_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(FigmaMcpError, match="FIGMA_MCP_TOKEN"):
                FigmaMcpClient.from_env()

    def test_raises_when_env_var_empty(self) -> None:
        """INVARIANT: from_env() raises FigmaMcpError when env var is empty string."""
        with patch.dict(os.environ, {"FIGMA_MCP_TOKEN": ""}):
            with pytest.raises(FigmaMcpError, match="FIGMA_MCP_TOKEN"):
                FigmaMcpClient.from_env()

    def test_custom_var_name(self) -> None:
        """INVARIANT: from_env() supports a custom environment variable name."""
        with patch.dict(os.environ, {"MY_FIGMA_TOKEN": "custom-token"}):
            client = FigmaMcpClient.from_env(var="MY_FIGMA_TOKEN")
        assert client._token == "custom-token"


class TestFromClaudeCredentials:
    def _write_creds(self, tmp_path: Path, data: dict[str, Any]) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / ".credentials.json").write_text(json.dumps(data))

    def test_reads_token_from_mcp_oauth(self, tmp_path: Path) -> None:
        """INVARIANT: from_claude_credentials() reads token from mcpOAuth (primary path)."""
        self._write_creds(tmp_path, {
            "mcpOAuth": {
                "plugin:figma:figma|abc123": {
                    "accessToken": "mcp-figma-token",
                    "refreshToken": "r",
                    "expiresAt": 9999999999,
                }
            }
        })
        with patch.object(Path, "home", return_value=tmp_path):
            client = FigmaMcpClient.from_claude_credentials()
        assert client._token == "mcp-figma-token"

    def test_falls_back_to_claude_ai_oauth_token(self, tmp_path: Path) -> None:
        """INVARIANT: from_claude_credentials() falls back to claudeAiOauthToken when
        mcpOAuth has no Figma key."""
        self._write_creds(tmp_path, {
            "mcpOAuth": {"plugin:slack:slack|xyz": {"accessToken": "slack-token"}},
            "claudeAiOauthToken": {"accessToken": "claude-oauth-token"},
        })
        with patch.object(Path, "home", return_value=tmp_path):
            client = FigmaMcpClient.from_claude_credentials()
        assert client._token == "claude-oauth-token"

    def test_raises_when_file_missing(self, tmp_path: Path) -> None:
        """INVARIANT: from_claude_credentials() raises FigmaMcpError when file absent."""
        with patch.object(Path, "home", return_value=tmp_path):
            with pytest.raises(FigmaMcpError, match="not found"):
                FigmaMcpClient.from_claude_credentials()

    def test_raises_when_file_is_invalid_json(self, tmp_path: Path) -> None:
        """INVARIANT: from_claude_credentials() raises FigmaMcpError on bad JSON."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text("not-json{{{")
        with patch.object(Path, "home", return_value=tmp_path):
            with pytest.raises(FigmaMcpError, match="Failed to parse"):
                FigmaMcpClient.from_claude_credentials()

    def test_raises_when_no_figma_token_anywhere(self, tmp_path: Path) -> None:
        """INVARIANT: from_claude_credentials() raises FigmaMcpError when no Figma token."""
        self._write_creds(tmp_path, {"other": "data"})
        with patch.object(Path, "home", return_value=tmp_path):
            with pytest.raises(FigmaMcpError, match="Could not find"):
                FigmaMcpClient.from_claude_credentials()


class TestAuto:
    def test_prefers_env_var_over_credentials(self, tmp_path: Path) -> None:
        """INVARIANT: auto() uses FIGMA_MCP_TOKEN when set, ignoring credentials."""
        with patch.dict(os.environ, {"FIGMA_MCP_TOKEN": "env-token"}):
            client = FigmaMcpClient.auto()
        assert client._token == "env-token"

    def test_falls_back_to_credentials_when_env_unset(self, tmp_path: Path) -> None:
        """INVARIANT: auto() reads from credentials file when env var is absent."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / ".credentials.json").write_text(json.dumps({
            "mcpOAuth": {
                "plugin:figma:figma|abc": {"accessToken": "cred-token"}
            }
        }))
        env = {k: v for k, v in os.environ.items() if k != "FIGMA_MCP_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(Path, "home", return_value=tmp_path):
                client = FigmaMcpClient.auto()
        assert client._token == "cred-token"


class TestUseFigma:
    @pytest.mark.asyncio
    async def test_sends_three_sequential_post_requests(self) -> None:
        """INVARIANT: use_figma() sends exactly initialize, notifications/initialized,
        and tools/call in order."""
        captured: list[httpx.Request] = []

        with _mock_three_step(capture=captured):
            client = FigmaMcpClient(_TOKEN)
            await client.use_figma(
                file_key=FILE_KEY,
                code="figma.currentPage.name",
                description="get page name",
            )

        assert len(captured) == 3
        methods = [json.loads(r.content)["method"] for r in captured]
        assert methods == ["initialize", "notifications/initialized", "tools/call"]

    @pytest.mark.asyncio
    async def test_uses_authorization_bearer_header(self) -> None:
        """INVARIANT: use_figma() authenticates with Authorization: Bearer, not X-Figma-Token.
        Also sends Accept: application/json, text/event-stream (MCP Streamable HTTP spec)."""
        captured: list[httpx.Request] = []

        with _mock_three_step(capture=captured):
            client = FigmaMcpClient(_TOKEN)
            await client.use_figma(file_key=FILE_KEY, code="1+1", description="test")

        init_req = captured[0]
        assert init_req.headers.get("authorization") == f"Bearer {_TOKEN}"
        assert "x-figma-token" not in init_req.headers
        assert "application/json" in (init_req.headers.get("accept") or "")
        assert "text/event-stream" in (init_req.headers.get("accept") or "")

    @pytest.mark.asyncio
    async def test_passes_session_id_in_subsequent_requests(self) -> None:
        """INVARIANT: notifications/initialized and tools/call include Mcp-Session-Id."""
        captured: list[httpx.Request] = []

        with _mock_three_step(capture=captured):
            client = FigmaMcpClient(_TOKEN)
            await client.use_figma(file_key=FILE_KEY, code="1+1", description="test")

        assert captured[1].headers.get("mcp-session-id") == SESSION_ID
        assert captured[2].headers.get("mcp-session-id") == SESSION_ID

    @pytest.mark.asyncio
    async def test_omits_session_id_when_initialize_returns_none(self) -> None:
        """INVARIANT: client supports stateless MCP servers with no session header."""
        captured: list[httpx.Request] = []

        with _mock_three_step(
            init_resp=httpx.Response(200, json=_init_response()),  # no session header
            capture=captured,
        ):
            client = FigmaMcpClient(_TOKEN)
            await client.use_figma(file_key=FILE_KEY, code="1+1", description="test")

        assert captured[1].headers.get("mcp-session-id") is None
        assert captured[2].headers.get("mcp-session-id") is None

    @pytest.mark.asyncio
    async def test_returns_result_from_tools_call(self) -> None:
        """INVARIANT: use_figma() returns the result dict from the tools/call response."""
        with _mock_three_step():
            client = FigmaMcpClient(_TOKEN)
            result = await client.use_figma(
                file_key=FILE_KEY,
                code="figma.currentPage.name",
                description="get page name",
            )

        assert result == _tools_call_response()["result"]


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_reuses_single_handshake_for_multiple_calls(self) -> None:
        """INVARIANT: open_session() initializes once and can run many tools/call requests."""
        captured: list[httpx.Request] = []
        responses: deque[httpx.Response] = deque([
            httpx.Response(200, json=_init_response(), headers={"Mcp-Session-Id": SESSION_ID}),
            httpx.Response(202),
            httpx.Response(200, json=_tools_call_response()),
            httpx.Response(200, json=_tools_call_response()),
            httpx.Response(204),
        ])

        def _side_effect(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return responses.popleft()

        with respx.mock() as router:
            router.route().mock(side_effect=_side_effect)
            client = FigmaMcpClient(_TOKEN)
            sess = await client.open_session()
            try:
                await sess.use_figma(FILE_KEY, "1+1", "first")
                await sess.use_figma(FILE_KEY, "2+2", "second")
            finally:
                await sess.close(best_effort=False)

        methods = [
            f"{req.method}:{json.loads(req.content)['method']}" if req.method == "POST" else req.method
            for req in captured
        ]
        assert methods == [
            "POST:initialize",
            "POST:notifications/initialized",
            "POST:tools/call",
            "POST:tools/call",
            "DELETE",
        ]

    @pytest.mark.asyncio
    async def test_closed_session_raises_on_use(self) -> None:
        """INVARIANT: closed session objects reject further use_figma calls."""
        with _mock_three_step():
            client = FigmaMcpClient(_TOKEN)
            sess = await client.open_session()
            await sess.close()
            with pytest.raises(FigmaMcpError, match="closed FigmaMcpSession"):
                await sess.use_figma(FILE_KEY, "1+1", "after close")

    @pytest.mark.asyncio
    async def test_passes_file_key_and_code_to_tools_call(self) -> None:
        """INVARIANT: use_figma() forwards file_key, code, and description to the MCP arguments."""
        captured: list[httpx.Request] = []

        with _mock_three_step(capture=captured):
            client = FigmaMcpClient(_TOKEN)
            await client.use_figma(
                file_key=FILE_KEY,
                code="const x = 42;",
                description="set x",
            )

        call_body = json.loads(captured[2].content)
        assert call_body["method"] == "tools/call"
        args = call_body["params"]["arguments"]
        assert args["fileKey"] == FILE_KEY
        assert args["code"] == "const x = 42;"
        assert args["description"] == "set x"

    @pytest.mark.asyncio
    async def test_raises_on_http_error_during_initialize(self) -> None:
        """INVARIANT: use_figma() raises FigmaMcpError on 401 during initialize."""
        with _mock_three_step(
            init_resp=httpx.Response(401, text="Unauthorized"),
        ):
            client = FigmaMcpClient(_TOKEN)
            with pytest.raises(FigmaMcpError, match="HTTP 401"):
                await client.use_figma(file_key=FILE_KEY, code="1+1", description="test")

    @pytest.mark.asyncio
    async def test_raises_on_jsonrpc_error_during_initialize(self) -> None:
        """INVARIANT: use_figma() raises FigmaMcpError on JSON-RPC error in initialize."""
        error_body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
        with _mock_three_step(
            init_resp=httpx.Response(
                200, json=error_body, headers={"Mcp-Session-Id": SESSION_ID}
            ),
        ):
            client = FigmaMcpClient(_TOKEN)
            with pytest.raises(FigmaMcpError, match="Invalid Request"):
                await client.use_figma(file_key=FILE_KEY, code="1+1", description="test")

    @pytest.mark.asyncio
    async def test_raises_on_http_error_during_tools_call(self) -> None:
        """INVARIANT: use_figma() raises FigmaMcpError on HTTP 500 during tools/call."""
        with _mock_three_step(
            call_resp=httpx.Response(500, text="Internal Server Error"),
        ):
            client = FigmaMcpClient(_TOKEN)
            with pytest.raises(FigmaMcpError, match="HTTP 500"):
                await client.use_figma(file_key=FILE_KEY, code="1+1", description="test")

    @pytest.mark.asyncio
    async def test_raises_on_jsonrpc_error_during_tools_call(self) -> None:
        """INVARIANT: use_figma() raises FigmaMcpError on JSON-RPC error in tools/call."""
        error_body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32601, "message": "Method not found"},
        }
        with _mock_three_step(
            call_resp=httpx.Response(200, json=error_body),
        ):
            client = FigmaMcpClient(_TOKEN)
            with pytest.raises(FigmaMcpError, match="Method not found"):
                await client.use_figma(file_key=FILE_KEY, code="1+1", description="test")

    @pytest.mark.asyncio
    async def test_parses_sse_response_from_initialize(self) -> None:
        """INVARIANT: use_figma() handles text/event-stream responses from initialize."""
        sse_body = f"event: message\ndata: {json.dumps(_init_response())}\n\n"
        with _mock_three_step(
            init_resp=httpx.Response(
                200,
                content=sse_body.encode(),
                headers={
                    "Content-Type": "text/event-stream",
                    "Mcp-Session-Id": SESSION_ID,
                },
            ),
        ):
            client = FigmaMcpClient(_TOKEN)
            result = await client.use_figma(
                file_key=FILE_KEY, code="1+1", description="test"
            )

        assert result == _tools_call_response()["result"]

    @pytest.mark.asyncio
    async def test_parses_sse_response_from_tools_call(self) -> None:
        """INVARIANT: use_figma() handles text/event-stream responses from tools/call."""
        sse_body = f"event: message\ndata: {json.dumps(_tools_call_response())}\n\n"
        with _mock_three_step(
            call_resp=httpx.Response(
                200,
                content=sse_body.encode(),
                headers={"Content-Type": "text/event-stream"},
            ),
        ):
            client = FigmaMcpClient(_TOKEN)
            result = await client.use_figma(
                file_key=FILE_KEY, code="1+1", description="test"
            )

        assert result == _tools_call_response()["result"]
