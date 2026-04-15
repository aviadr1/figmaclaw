from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from figmaclaw.main import cli


def test_stream_format_ok_and_tool_lines(tmp_path: Path, monkeypatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    msgs = [
        {
            "type": "system",
            "model": "gpt",
            "tools": ["Bash", "Read"],
            "mcp_servers": [{"name": "plugin:figma", "status": "connected"}],
            "permissionMode": "default",
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "x.md"}},
                ]
            },
        },
        {"type": "result", "num_turns": 2, "duration_ms": 1200, "total_cost_usd": 0.1234},
    ]
    payload = "\n".join(json.dumps(m) for m in msgs) + "\n"

    runner = CliRunner()
    result = runner.invoke(cli, ["stream-format"], input=payload)

    assert result.exit_code == 0
    assert "[init]" in result.output
    assert "  > Bash: echo hi" in result.output
    assert "  > Read: x.md" in result.output
    assert "[result] status=ok" in result.output
    text = summary.read_text()
    assert "claude-run result" in text
    assert "$0.1234" in text


def test_stream_format_error_exit_and_invalid_json_passthrough(tmp_path: Path, monkeypatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    msgs = [
        "not-json",
        json.dumps({"type": "error", "error": "boom"}),
        json.dumps({"type": "result", "is_error": True, "result": "failed", "duration_ms": 10}),
    ]
    payload = "\n".join(msgs) + "\n"

    runner = CliRunner()
    result = runner.invoke(cli, ["stream-format"], input=payload)

    assert result.exit_code == 1
    assert "not-json" in result.output
    assert "[ERROR] boom" in result.output
    assert "[error] failed" in result.output
    assert "Errors" in summary.read_text()
