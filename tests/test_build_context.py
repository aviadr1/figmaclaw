"""Tests for commands/build_context.py.

INVARIANTS:
- build-context maps typed context export failures to categorized CLI errors
- build-context emits JSON call specs on success
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from figmaclaw.in_context import ContextBuildError, ContextErrorCategory
from figmaclaw.main import cli


def _write_source_md(tmp_path: Path) -> Path:
    p = tmp_path / "source.md"
    p.write_text(
        """---
file_key: SRC_FILE_KEY
page_node_id: 7423:8435
frame_sections:
  '7424:15980':
    - {node_id: '7424:16018', name: Header, x: 0, y: 0, w: 393, h: 116}
---
"""
    )
    return p


def _args(source_md: Path) -> list[str]:
    return [
        "build-context",
        "--source-md",
        str(source_md),
        "--source-frame",
        "7424:15980",
        "--target-file",
        "DRAFT_FILE",
        "--target-page",
        "18:7",
        "--comp-node",
        "18:20",
        "--comp-x",
        "80",
        "--comp-y",
        "498",
        "--comp-w",
        "76",
        "--label",
        "Caption",
    ]


def test_build_context_surfaces_error_category(tmp_path: Path) -> None:
    """INVARIANT: typed context failures are surfaced as [CATEGORY] CLI errors."""
    source_md = _write_source_md(tmp_path)
    runner = CliRunner()

    with patch(
        "figmaclaw.commands.build_context._run",
        AsyncMock(
            side_effect=ContextBuildError(
                ContextErrorCategory.NO_EXPORT_URL,
                "No export URL returned for section '7424:16018'.",
            )
        ),
    ):
        result = runner.invoke(
            cli,
            _args(source_md),
            env={"FIGMA_API_KEY": "fake"},
        )

    assert result.exit_code != 0
    assert "[NO_EXPORT_URL]" in result.output
    assert "No export URL returned for section" in result.output


def test_build_context_emits_json_calls_on_success(tmp_path: Path) -> None:
    """INVARIANT: successful build-context output is valid JSON call-spec array."""
    source_md = _write_source_md(tmp_path)
    runner = CliRunner()

    fake_calls = [
        {
            "file_key": "DRAFT_FILE",
            "description": "Create context container frame 'ctx-7424-15980-18-20'",
            "code": "createContextContainer(...)",
        }
    ]

    with patch("figmaclaw.commands.build_context._run", AsyncMock(return_value=fake_calls)):
        result = runner.invoke(
            cli,
            _args(source_md),
            env={"FIGMA_API_KEY": "fake"},
        )

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert isinstance(parsed, list)
    assert parsed[0]["file_key"] == "DRAFT_FILE"
    assert "createContextContainer" in parsed[0]["code"]
