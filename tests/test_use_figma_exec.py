"""Tests for figmaclaw.use_figma_exec shared MCP call executor.

INVARIANTS:
- dry-run reports planned calls without opening MCP session
- execute mode preserves order and counts failures from isError results
- continue_on_error controls fail-fast vs best-effort behavior
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from figmaclaw.use_figma_exec import execute_use_figma_calls


def _calls() -> list[dict[str, str]]:
    return [
        {"file_key": "F", "code": "1+1", "description": "first"},
        {"file_key": "F", "code": "2+2", "description": "second"},
        {"file_key": "F", "code": "3+3", "description": "third"},
    ]


class _SessionCM:
    def __init__(self, use_figma_fn: Callable[..., Any]) -> None:
        self._use_figma_fn = use_figma_fn

    async def __aenter__(self) -> Any:
        class _Sess:
            async def use_figma(self, *, file_key: str, code: str, description: str) -> Any:
                return await self._use_figma_fn(
                    file_key=file_key,
                    code=code,
                    description=description,
                )

            def __init__(self, use_figma_fn: Callable[..., Any]) -> None:
                self._use_figma_fn = use_figma_fn

        return _Sess(self._use_figma_fn)

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


class _FakeMcp:
    def __init__(self, use_figma_fn: Callable[..., Any]) -> None:
        self._use_figma_fn = use_figma_fn

    def session(self) -> _SessionCM:
        return _SessionCM(self._use_figma_fn)


@pytest.mark.asyncio
async def test_dry_run_returns_planned_subset() -> None:
    """INVARIANT: dry-run returns only calls at/after resume_from, without MCP IO."""
    result = await execute_use_figma_calls(_calls(), resume_from=1, dry_run=True)
    assert result["mode"] == "dry-run"
    assert result["total"] == 3
    assert result["planned"] == 2
    assert [c["description"] for c in result["calls"]] == ["second", "third"]


@pytest.mark.asyncio
async def test_negative_resume_from_rejected() -> None:
    """INVARIANT: resume_from must be >= 0."""
    with pytest.raises(ValueError, match="resume_from must be >= 0"):
        await execute_use_figma_calls(_calls(), resume_from=-1)


@pytest.mark.asyncio
async def test_execute_counts_iserror_results_as_failures() -> None:
    """INVARIANT: execute mode keeps order and counts tool-level isError results."""
    use_figma = AsyncMock(
        side_effect=[
            {"isError": False, "content": [{"text": "ok1"}]},
            {"isError": True, "content": [{"text": "bad"}]},
        ]
    )
    fake_mcp = _FakeMcp(use_figma)

    with patch("figmaclaw.use_figma_exec.FigmaMcpClient.auto", return_value=fake_mcp):
        result = await execute_use_figma_calls(_calls()[:2], dry_run=False)

    assert result["mode"] == "execute"
    assert result["executed"] == 2
    assert result["failures"] == 1
    assert [c["description"] for c in result["calls"]] == ["first", "second"]
    assert result["calls"][1]["isError"] is True


@pytest.mark.asyncio
async def test_execute_raises_immediately_when_continue_on_error_false() -> None:
    """INVARIANT: executor fail-fast behavior re-raises first MCP exception."""
    use_figma = AsyncMock(side_effect=[RuntimeError("boom")])
    fake_mcp = _FakeMcp(use_figma)

    with (
        patch("figmaclaw.use_figma_exec.FigmaMcpClient.auto", return_value=fake_mcp),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await execute_use_figma_calls(_calls()[:1], continue_on_error=False)


@pytest.mark.asyncio
async def test_execute_continues_after_exception_when_requested() -> None:
    """INVARIANT: continue_on_error captures exceptions and proceeds to later calls."""
    use_figma = AsyncMock(
        side_effect=[
            RuntimeError("first failed"),
            {"isError": False, "content": [{"text": "ok"}]},
        ]
    )
    fake_mcp = _FakeMcp(use_figma)

    with patch("figmaclaw.use_figma_exec.FigmaMcpClient.auto", return_value=fake_mcp):
        result = await execute_use_figma_calls(_calls()[:2], continue_on_error=True)

    assert result["executed"] == 2
    assert result["failures"] == 1
    assert result["calls"][0]["isError"] is True
    assert "first failed" in result["calls"][0]["error"]
    assert result["calls"][1]["isError"] is False
