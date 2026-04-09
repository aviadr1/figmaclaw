"""Figma MCP client — Python-callable use_figma without Claude relay.

Sends the three-step MCP handshake directly to https://mcp.figma.com/mcp:
  1. initialize
  2. notifications/initialized
  3. tools/call (use_figma)

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
from pathlib import Path
from typing import Any

import httpx

_MCP_URL = "https://mcp.figma.com/mcp"
_CLIENT_NAME = "figmaclaw"
_CLIENT_VERSION = "1.0"


class FigmaMcpError(Exception):
    """Raised when the Figma MCP server returns an error or token is missing."""


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
            raise FigmaMcpError(
                f"Environment variable {var!r} is not set or empty."
            )
        return cls(token)

    @classmethod
    def from_claude_credentials(cls) -> FigmaMcpClient:
        """Read the OAuth token from ``~/.claude/.credentials.json``.

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
            raise FigmaMcpError(
                f"Failed to parse {cred_path}: {exc}"
            ) from exc

        # Structure: {"claudeAiOauthToken": {"accessToken": "...", ...}, ...}
        token_obj = data.get("claudeAiOauthToken") or {}
        token = (token_obj.get("accessToken") or "").strip()
        if not token:
            raise FigmaMcpError(
                f"Could not find OAuth access token in {cred_path}.\n"
                "Expected key path: data['claudeAiOauthToken']['accessToken']"
            )
        return cls(token)

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

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

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
        async with httpx.AsyncClient(timeout=60.0) as client:
            session_id = await self._initialize(client)
            await self._notify_initialized(client, session_id)
            return await self._call_use_figma(
                client, session_id, file_key, code, description
            )

    async def _initialize(self, client: httpx.AsyncClient) -> str:
        """Send the MCP initialize request and return the session ID."""
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
        resp = await client.post(_MCP_URL, json=payload, headers=self._auth_header())
        _check_http(resp, "initialize")
        body: dict[str, Any] = resp.json()
        _check_jsonrpc_error(body, "initialize")

        session_id = resp.headers.get("Mcp-Session-Id", "").strip()
        if not session_id:
            raise FigmaMcpError(
                "Figma MCP did not return a Mcp-Session-Id header "
                "in the initialize response."
            )
        return session_id

    async def _notify_initialized(
        self, client: httpx.AsyncClient, session_id: str
    ) -> None:
        """Send the notifications/initialized notification (no id — fire-and-forget)."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        resp = await client.post(
            _MCP_URL,
            json=payload,
            headers={**self._auth_header(), "Mcp-Session-Id": session_id},
        )
        _check_http(resp, "notifications/initialized")

    async def _call_use_figma(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        file_key: str,
        code: str,
        description: str,
    ) -> dict[str, Any]:
        """Send the tools/call request for the use_figma tool."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "use_figma",
                "arguments": {
                    "fileKey": file_key,
                    "code": code,
                    "description": description,
                },
            },
        }
        resp = await client.post(
            _MCP_URL,
            json=payload,
            headers={**self._auth_header(), "Mcp-Session-Id": session_id},
        )
        _check_http(resp, "tools/call")
        body: dict[str, Any] = resp.json()
        _check_jsonrpc_error(body, "tools/call")
        result: dict[str, Any] = body.get("result", {})
        return result


def _check_http(resp: httpx.Response, step: str) -> None:
    """Raise FigmaMcpError on non-2xx HTTP responses."""
    if resp.status_code >= 400:
        raise FigmaMcpError(
            f"MCP {step!r} failed: HTTP {resp.status_code}\n{resp.text[:500]}"
        )


def _check_jsonrpc_error(body: dict[str, Any], step: str) -> None:
    """Raise FigmaMcpError if the JSON-RPC body contains an error field."""
    if "error" in body:
        err = body["error"]
        code = err.get("code", "?")
        msg = err.get("message", str(err))
        raise FigmaMcpError(f"MCP {step!r} returned error {code}: {msg}")
