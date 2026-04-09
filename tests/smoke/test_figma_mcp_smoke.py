"""Smoke tests for FigmaMcpClient against the real Figma MCP endpoint.

Requires a valid OAuth token (FIGMA_MCP_TOKEN env var or ~/.claude/.credentials.json).
Run with:
    uv run pytest -m smoke tests/smoke/test_figma_mcp_smoke.py -v
"""

from __future__ import annotations

import pytest

from figmaclaw.figma_mcp import FigmaMcpClient, FigmaMcpError

# Web App file used in linear-git
TEST_FILE_KEY = "hOV4QMBnDIG5s5OYkSrX9E"


@pytest.fixture
def mcp_client() -> FigmaMcpClient:
    try:
        return FigmaMcpClient.auto()
    except FigmaMcpError as exc:
        pytest.skip(f"No MCP credentials available: {exc}")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_use_figma_returns_page_count(mcp_client: FigmaMcpClient) -> None:
    """Smoke: use_figma() executes JS in Figma and returns a real result."""
    result = await mcp_client.use_figma(
        file_key=TEST_FILE_KEY,
        code="figma.root.children.length",
        description="count pages in the file",
    )
    assert result is not None, "result must not be None"
    assert not result.get("isError", False), f"MCP returned isError=True: {result}"
