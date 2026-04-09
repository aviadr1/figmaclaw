"""Shared executor for batches of ``use_figma`` MCP calls.

This module is intentionally generic so multiple commands can reuse it:
- in-context execute (#43)
- apply-tokens execute path (#42)
"""

from __future__ import annotations

from typing import Any

from figmaclaw.figma_mcp import FigmaMcpClient


async def execute_use_figma_calls(
    calls: list[dict[str, str]],
    *,
    resume_from: int = 0,
    continue_on_error: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute ``use_figma`` calls in order and return a structured summary.

    Parameters
    ----------
    calls:
        List of call dicts with keys: ``file_key``, ``code``, ``description``.
    resume_from:
        0-based index to start execution from.
    continue_on_error:
        If true, keep going after call failures and collect them in summary.
        If false, abort on first failure by re-raising the underlying exception.
    dry_run:
        If true, do not call MCP; only return the planned execution list.
    """
    if resume_from < 0:
        raise ValueError("resume_from must be >= 0")

    planned = calls[resume_from:]
    if dry_run:
        return {
            "mode": "dry-run",
            "total": len(calls),
            "resume_from": resume_from,
            "planned": len(planned),
            "calls": [
                {
                    "index": i,
                    "description": c.get("description", ""),
                    "file_key": c.get("file_key", ""),
                }
                for i, c in enumerate(calls, start=1)
                if i - 1 >= resume_from
            ],
        }

    mcp = FigmaMcpClient.auto()
    out: list[dict[str, Any]] = []
    failures = 0

    async with mcp.session() as sess:
        for idx0, call in enumerate(calls):
            if idx0 < resume_from:
                continue
            idx = idx0 + 1
            try:
                result = await sess.use_figma(
                    file_key=call["file_key"],
                    code=call["code"],
                    description=call["description"],
                )
                is_error = bool(result.get("isError", False))
                if is_error:
                    failures += 1
                out.append(
                    {
                        "index": idx,
                        "description": call["description"],
                        "isError": is_error,
                        "result": result,
                    }
                )
            except Exception as exc:
                failures += 1
                if not continue_on_error:
                    raise
                out.append(
                    {
                        "index": idx,
                        "description": call.get("description", ""),
                        "isError": True,
                        "error": str(exc),
                    }
                )

    return {
        "mode": "execute",
        "total": len(calls),
        "resume_from": resume_from,
        "executed": len(out),
        "failures": failures,
        "calls": out,
    }
