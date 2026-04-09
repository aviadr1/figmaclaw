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


def test_build_context_execute_invokes_shared_executor(tmp_path: Path) -> None:
    """INVARIANT: --execute runs generated calls through the shared use_figma executor."""
    source_md = _write_source_md(tmp_path)
    runner = CliRunner()

    fake_calls = [
        {"file_key": "DRAFT_FILE", "description": "first", "code": "1+1"},
        {"file_key": "DRAFT_FILE", "description": "second", "code": "2+2"},
    ]
    fake_summary = {"mode": "execute", "executed": 2, "failures": 0}

    with (
        patch("figmaclaw.commands.build_context._run", AsyncMock(return_value=fake_calls)),
        patch(
            "figmaclaw.commands.build_context.execute_use_figma_calls",
            AsyncMock(return_value=fake_summary),
        ) as exec_mock,
    ):
        result = runner.invoke(
            cli,
            _args(source_md) + ["--execute", "--resume-from", "1", "--continue-on-error"],
            env={"FIGMA_API_KEY": "fake"},
        )

    assert result.exit_code == 0
    assert json.loads(result.output) == fake_summary
    exec_mock.assert_awaited_once_with(
        fake_calls,
        resume_from=1,
        continue_on_error=True,
        dry_run=False,
    )


def test_build_context_rejects_execute_only_flags_without_execute(tmp_path: Path) -> None:
    """INVARIANT: --resume-from and --continue-on-error require --execute."""
    source_md = _write_source_md(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        _args(source_md) + ["--resume-from", "1"],
        env={"FIGMA_API_KEY": "fake"},
    )

    assert result.exit_code != 0
    assert "--resume-from/--continue-on-error require --execute" in result.output
