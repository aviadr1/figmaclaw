"""Figma MCP client — Python-callable use_figma without Claude relay.

Sends the three-step MCP handshake directly to https://mcp.figma.com/mcp:
  1. initialize
  2. notifications/initialized
  3. tools/call (use_figma)

Some MCP deployments return an ``Mcp-Session-Id`` header from initialize and
expect it on subsequent calls; others are currently stateless. This client
supports both modes.

Token discovery order (via FigmaMcpClient.auto()):
  1. FIGMA_MCP_TOKEN environment variable
  2. OAuth token from ~/.claude/.credentials.json

Usage::

    client = FigmaMcpClient.auto()
    result = await client.use_figma(
        file_key="ABC123",
        code="figma.currentPage.name",
        description="get current page name",
    )
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

_MCP_URL = "https://mcp.figma.com/mcp"
_CLIENT_NAME = "figmaclaw"
_CLIENT_VERSION = "1.0"


class FigmaMcpError(Exception):
    """Raised when the Figma MCP server returns an error or token is missing."""


class FigmaMcpSession:
    """Reusable MCP session for multiple ``use_figma`` calls.

    Sessions support both deployment modes:
    - sessionful (initialize returns ``Mcp-Session-Id``)
    - stateless (no session ID returned)
    """

    def __init__(
        self,
        owner: FigmaMcpClient,
        client: httpx.AsyncClient,
        session_id: str | None,
    ) -> None:
        self._owner = owner
        self._client = client
        self._session_id = session_id
        self._closed = False

    @property
    def session_id(self) -> str | None:
        """Current MCP session ID when provided by the server, else ``None``."""
        return self._session_id

    @property
    def is_sessionful(self) -> bool:
        """Whether this connection has a server-assigned MCP session ID."""
        return bool(self._session_id)

    @property
    def is_closed(self) -> bool:
        """Whether ``close()`` has been called."""
        return self._closed

    async def use_figma(
        self,
        file_key: str,
        code: str,
        description: str,
    ) -> dict[str, Any]:
        """Execute a ``tools/call use_figma`` request on this open session."""
        if self._closed:
            raise FigmaMcpError("Cannot use a closed FigmaMcpSession.")
        return await self._owner._call_use_figma(
            self._client,
            self._session_id,
            file_key,
            code,
            description,
        )

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute an arbitrary MCP tool on this open session."""
        if self._closed:
            raise FigmaMcpError("Cannot use a closed FigmaMcpSession.")
        return await self._owner._call_tool(
            self._client,
            self._session_id,
            name,
            arguments or {},
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return tools exposed by the MCP server for this authenticated client."""
        if self._closed:
            raise FigmaMcpError("Cannot use a closed FigmaMcpSession.")
        return await self._owner._list_tools(self._client, self._session_id)

    async def refresh(self) -> str | None:
        """Re-run MCP initialize + initialized notification on the same connection."""
        if self._closed:
            raise FigmaMcpError("Cannot refresh a closed FigmaMcpSession.")
        self._session_id = await self._owner._initialize(self._client)
        await self._owner._notify_initialized(self._client, self._session_id)
        return self._session_id

    async def close(self, *, best_effort: bool = True) -> None:
        """Close the underlying HTTP client.

        For sessionful servers this also attempts to terminate the MCP session
        with ``DELETE /mcp``. If termination is unsupported, close still
        succeeds by default (``best_effort=True``).
        """
        if self._closed:
            return
        try:
            if self._session_id:
                try:
                    await self._owner._terminate_session(
                        self._client,
                        self._session_id,
                        best_effort=best_effort,
                    )
                except Exception:
                    if not best_effort:
                        raise
        finally:
            self._closed = True
            await self._client.aclose()


class FigmaMcpClient:
    """Python client for the Figma MCP server.

    Executes JavaScript in Figma's plugin sandbox via the official MCP
    endpoint, without needing Claude as a relay.

    Create with one of the class-method constructors:

    - ``FigmaMcpClient.from_env()`` — use ``FIGMA_MCP_TOKEN`` env var
    - ``FigmaMcpClient.from_claude_credentials()`` — read from Claude's credential store
    - ``FigmaMcpClient.auto()`` — try env var first, fall back to Claude credentials
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @classmethod
    def from_env(cls, var: str = "FIGMA_MCP_TOKEN") -> FigmaMcpClient:
        """Read the OAuth token from an environment variable.

        Parameters
        ----------
        var:
            Environment variable name (default: ``FIGMA_MCP_TOKEN``).

        Raises
        ------
        FigmaMcpError
            If the environment variable is not set or empty.
        """
        token = os.environ.get(var, "").strip()
        if not token:
            raise FigmaMcpError(f"Environment variable {var!r} is not set or empty.")
        return cls(token)

    @classmethod
    def from_claude_credentials(cls) -> FigmaMcpClient:
        """Read the Figma OAuth token from ``~/.claude/.credentials.json``.

        Lookup order:
        1. ``mcpOAuth`` — any key whose name contains ``"figma"`` (case-insensitive).
           This is where Claude Code stores per-server MCP OAuth tokens.
           Key format: ``"plugin:figma:figma|<hash>"``.
        2. ``claudeAiOauthToken`` / ``claudeAiOauth`` — legacy / fallback paths.

        Raises
        ------
        FigmaMcpError
            If the credentials file is missing, unparseable, or lacks a token.
        """
        cred_path = Path.home() / ".claude" / ".credentials.json"
        if not cred_path.exists():
            raise FigmaMcpError(
                f"Claude credentials file not found: {cred_path}\n"
                "Log in to Claude Code first or set FIGMA_MCP_TOKEN."
            )
        try:
            data: dict[str, Any] = json.loads(cred_path.read_text())
        except Exception as exc:
            raise FigmaMcpError(f"Failed to parse {cred_path}: {exc}") from exc

        # Primary: mcpOAuth[<any key containing "figma">]["accessToken"]
        mcp_oauth: dict[str, Any] = data.get("mcpOAuth") or {}
        for key, entry in mcp_oauth.items():
            if "figma" in key.lower() and isinstance(entry, dict):
                token = (entry.get("accessToken") or "").strip()
                if token:
                    return cls(token)

        for fallback_key in ("claudeAiOauthToken", "claudeAiOauth"):
            token_obj: dict[str, Any] = data.get(fallback_key) or {}
            token = (token_obj.get("accessToken") or "").strip()
            if token:
                return cls(token)

        raise FigmaMcpError(
            f"Could not find a Figma OAuth access token in {cred_path}.\n"
            "Expected: mcpOAuth['plugin:figma:figma|...']['accessToken'] "
            "or claudeAiOauthToken['accessToken'] / claudeAiOauth['accessToken']"
        )

    @classmethod
    def auto(cls) -> FigmaMcpClient:
        """Try ``FIGMA_MCP_TOKEN`` first, then Claude credentials file.

        Raises
        ------
        FigmaMcpError
            If neither source provides a token.
        """
        token = os.environ.get("FIGMA_MCP_TOKEN", "").strip()
        if token:
            return cls(token)
        return cls.from_claude_credentials()

    def _base_headers(self) -> dict[str, str]:
        # MCP Streamable HTTP requires the client to advertise both transports.
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, text/event-stream",
        }

    async def open_session(self, timeout: float = 60.0) -> FigmaMcpSession:
        """Create a reusable MCP session (or stateless connection)."""
        client = httpx.AsyncClient(timeout=timeout)
        try:
            session_id = await self._initialize(client)
            await self._notify_initialized(client, session_id)
            return FigmaMcpSession(self, client, session_id)
        except Exception:
            await client.aclose()
            raise

    @asynccontextmanager
    async def session(self, timeout: float = 60.0) -> AsyncIterator[FigmaMcpSession]:
        """Context manager for automatic session open/close."""
        sess = await self.open_session(timeout=timeout)
        try:
            yield sess
        finally:
            await sess.close()

    async def use_figma(
        self,
        file_key: str,
        code: str,
        description: str,
    ) -> dict[str, Any]:
        """Execute JavaScript *code* in Figma's plugin sandbox.

        Performs the three-step MCP handshake (initialize →
        notifications/initialized → tools/call) using the same HTTP session.

        Parameters
        ----------
        file_key:
            Figma file key (from the Figma URL, e.g. ``"ABC123"``).
        code:
            JavaScript source to run inside the Figma plugin sandbox.
        description:
            Human-readable description of what the code does.

        Returns
        -------
        dict
            The ``result`` field from the MCP ``tools/call`` JSON-RPC response.

        Raises
        ------
        FigmaMcpError
            If the MCP server returns an HTTP error or a JSON-RPC error at
            any step.
        """
        async with self.session(timeout=60.0) as sess:
            return await sess.use_figma(file_key, code, description)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute an arbitrary MCP ``tools/call`` request."""
        async with self.session(timeout=60.0) as sess:
            return await sess.call_tool(name, arguments or {})

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP server tool list for the authenticated client."""
        async with self.session(timeout=60.0) as sess:
            return await sess.list_tools()

    async def _initialize(self, client: httpx.AsyncClient) -> str | None:
        """Send the MCP initialize request and return the optional session ID."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": _CLIENT_NAME,
                    "version": _CLIENT_VERSION,
                },
            },
        }
        resp = await client.post(_MCP_URL, json=payload, headers=self._base_headers())
        _check_http(resp, "initialize")
        body: dict[str, Any] = _parse_body(resp, "initialize")
        _check_jsonrpc_error(body, "initialize")

        # Some deployments provide MCP session IDs via response headers while
        # others currently operate statelessly with no session header at all.
        session_id = resp.headers.get("Mcp-Session-Id", "").strip()
        return session_id or None

    async def _notify_initialized(self, client: httpx.AsyncClient, session_id: str | None) -> None:
        """Send the notifications/initialized notification (no id — fire-and-forget)."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        headers = self._base_headers()
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        resp = await client.post(
            _MCP_URL,
            json=payload,
            headers=headers,
        )
        _check_http(resp, "notifications/initialized")

    async def _call_use_figma(
        self,
        client: httpx.AsyncClient,
        session_id: str | None,
        file_key: str,
        code: str,
        description: str,
    ) -> dict[str, Any]:
        """Send the tools/call request for the use_figma tool."""
        return await self._call_tool(
            client,
            session_id,
            "use_figma",
            {
                "fileKey": file_key,
                "code": code,
                "description": description,
            },
        )

    async def _call_tool(
        self,
        client: httpx.AsyncClient,
        session_id: str | None,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a generic tools/call request."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        }
        headers = self._base_headers()
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        resp = await client.post(
            _MCP_URL,
            json=payload,
            headers=headers,
        )
        _check_http(resp, "tools/call")
        body: dict[str, Any] = _parse_body(resp, "tools/call")
        _check_jsonrpc_error(body, "tools/call")
        result: dict[str, Any] = body.get("result", {})
        return result

    async def _list_tools(
        self,
        client: httpx.AsyncClient,
        session_id: str | None,
    ) -> list[dict[str, Any]]:
        """Send a tools/list request."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        headers = self._base_headers()
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        resp = await client.post(
            _MCP_URL,
            json=payload,
            headers=headers,
        )
        _check_http(resp, "tools/list")
        body: dict[str, Any] = _parse_body(resp, "tools/list")
        _check_jsonrpc_error(body, "tools/list")
        result = body.get("result", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise FigmaMcpError("MCP 'tools/list' returned an invalid tools payload")
        return tools

    async def _terminate_session(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        *,
        best_effort: bool,
    ) -> None:
        """Best-effort MCP session termination for sessionful servers."""
        resp = await client.delete(
            _MCP_URL,
            headers={**self._base_headers(), "Mcp-Session-Id": session_id},
        )
        # Many servers/deployments don't implement explicit termination.
        if resp.status_code in (404, 405, 501):
            return
        if resp.status_code >= 400 and not best_effort:
            raise FigmaMcpError(
                f"MCP session termination failed: HTTP {resp.status_code}\n{resp.text[:500]}"
            )


def _check_http(resp: httpx.Response, step: str) -> None:
    """Raise FigmaMcpError on non-2xx HTTP responses."""
    if resp.status_code >= 400:
        raise FigmaMcpError(f"MCP {step!r} failed: HTTP {resp.status_code}\n{resp.text[:500]}")


def _parse_body(resp: httpx.Response, step: str) -> dict[str, Any]:
    """Parse the response body, handling both JSON and SSE (text/event-stream) formats.

    The Figma MCP server may respond with either Content-Type depending on the
    Accept header negotiation. SSE lines look like::

        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}

    """
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                json_str = line[5:].strip()
                if json_str:
                    return json.loads(json_str)  # type: ignore[no-any-return]
        raise FigmaMcpError(f"MCP {step!r} returned SSE with no data line:\n{resp.text[:300]}")
    return resp.json()  # type: ignore[no-any-return]


def _check_jsonrpc_error(body: dict[str, Any], step: str) -> None:
    """Raise FigmaMcpError if the JSON-RPC body contains an error field."""
    if "error" in body:
        err = body["error"]
        code = err.get("code", "?")
        msg = err.get("message", str(err))
        raise FigmaMcpError(f"MCP {step!r} returned error {code}: {msg}")
