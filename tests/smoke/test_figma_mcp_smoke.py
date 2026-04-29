"""Smoke tests for FigmaMcpClient against the real Figma MCP endpoint.

Requires a valid OAuth token (FIGMA_MCP_TOKEN env var or ~/.claude/.credentials.json).
Run with:
    uv run pytest -m smoke_mcp tests/smoke/test_figma_mcp_smoke.py -v
"""

from __future__ import annotations

import pytest

from figmaclaw.figma_mcp import FigmaMcpClient, FigmaMcpError
from figmaclaw.figma_variables_mcp import get_local_variables_via_mcp
from tests.smoke.live_gate import require_live_credential

# Web App file used in linear-git
TEST_FILE_KEY = "hOV4QMBnDIG5s5OYkSrX9E"
DS_FILE_KEY = "dcDETwKMNGpK39FfApg7Ki"


@pytest.fixture
def mcp_client() -> FigmaMcpClient:
    """Build an MCP client using the same lookup chain as production code.

    ``FigmaMcpClient.auto()`` reads ``FIGMA_MCP_TOKEN`` first, then falls
    back to ``~/.claude/.credentials.json`` (where Claude Code stores the
    Figma plugin's OAuth token). Devs who've authenticated the Figma plugin
    in Claude Code get the smoke tests running automatically — no need to
    copy a token into ``.env``.

    If neither source has a token, route through the live-credential gate
    so CI's dedicated smoke job (``FIGMACLAW_REQUIRE_LIVE_SMOKE=1``) fails
    loudly while local runs skip.
    """
    try:
        return FigmaMcpClient.auto()
    except FigmaMcpError as exc:
        require_live_credential(
            "",
            name="FIGMA_MCP_TOKEN",
            hint=(
                "No MCP token found via env var or ~/.claude/.credentials.json. "
                "Authenticate the Figma plugin in Claude Code, or set "
                f"FIGMA_MCP_TOKEN. Original error: {exc}"
            ),
        )
        raise  # unreachable: require_live_credential always raises


@pytest.mark.smoke_mcp
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


@pytest.mark.smoke_mcp
@pytest.mark.asyncio
async def test_session_reuse_makes_two_calls(mcp_client: FigmaMcpClient) -> None:
    """Smoke: session() context manager can execute multiple tools/call requests
    without re-running the MCP handshake."""
    async with mcp_client.session() as sess:
        r1 = await sess.use_figma(
            file_key=TEST_FILE_KEY,
            code="figma.root.children.length",
            description="count pages (call 1)",
        )
        r2 = await sess.use_figma(
            file_key=TEST_FILE_KEY,
            code="figma.root.name",
            description="get document name (call 2)",
        )

    assert not r1.get("isError", False), f"call 1 returned isError=True: {r1}"
    assert not r2.get("isError", False), f"call 2 returned isError=True: {r2}"


@pytest.mark.smoke_mcp
@pytest.mark.asyncio
async def test_mcp_exposes_read_variable_tools(mcp_client: FigmaMcpClient) -> None:
    """Smoke: Figma MCP exposes read-tool alternatives to use_figma."""
    tools = await mcp_client.list_tools()
    tool_names = {tool.get("name") for tool in tools}

    assert "get_variable_defs" in tool_names
    assert "search_design_system" in tool_names


@pytest.mark.smoke_mcp
@pytest.mark.asyncio
async def test_mcp_exports_design_system_variable_definitions(
    mcp_client: FigmaMcpClient,
) -> None:
    """Smoke: MCP returns actual DS variable names/collections/modes."""
    try:
        response = await get_local_variables_via_mcp(DS_FILE_KEY, client=mcp_client)
    except FigmaMcpError as exc:
        if "read-only mode" in str(exc).lower():
            pytest.xfail(
                "Figma MCP use_figma denied plugin-runtime variable export in read-only mode. "
                "This is a live file/tool capability denial; figmaclaw variables handles it "
                "as unavailable instead of poisoning the catalog."
            )
        raise

    assert response.meta.variables, "MCP export returned no variables"
    assert response.meta.variableCollections, "MCP export returned no collections"
    assert any(v.name for v in response.meta.variables.values()), "variables have no names"
    assert any(c.modes for c in response.meta.variableCollections.values()), (
        "collections have no modes"
    )
