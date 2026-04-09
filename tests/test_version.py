"""Tests for the figmaclaw --version flag.

INVARIANTS:
- --version exits with code 0
- Output shows "figmaclaw <version> (<8-char-sha>)"
- Output shows first line of commit message on a second line
- When __commit__ is empty, shows "unknown" instead of sha
- When __pr__ is set, shows "· PR #<n>" in the header line
- Only the first line of a multi-line commit message is shown
- --version does not require FIGMA_API_KEY or any other env vars
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

import figmaclaw._build_info as _build_info
from figmaclaw.main import cli


def test_version_shows_version_and_short_sha() -> None:
    """INVARIANT: --version shows 'figmaclaw <version> (<8-char-sha>)'."""
    runner = CliRunner()
    with (
        patch.object(_build_info, "__version__", "1.2.3"),
        patch.object(_build_info, "__commit__", "abc1234567890def"),
        patch.object(_build_info, "__commit_message__", "feat: something"),
        patch.object(_build_info, "__pr__", None),
    ):
        result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "figmaclaw 1.2.3 (abc12345)" in result.output


def test_version_shows_commit_message_first_line() -> None:
    """INVARIANT: First line of the commit message is shown on the second output line."""
    runner = CliRunner()
    with (
        patch.object(_build_info, "__version__", "1.0.0"),
        patch.object(_build_info, "__commit__", "deadbeef12345678"),
        patch.object(
            _build_info,
            "__commit_message__",
            "fix: repair the thing\n\nLonger body that must not appear.",
        ),
        patch.object(_build_info, "__pr__", None),
    ):
        result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "fix: repair the thing" in result.output
    assert "Longer body that must not appear" not in result.output


def test_version_shows_pr_number_for_pr_merge() -> None:
    """INVARIANT: When __pr__ is set, the header includes '· PR #<n>'."""
    runner = CliRunner()
    with (
        patch.object(_build_info, "__version__", "2.0.0"),
        patch.object(_build_info, "__commit__", "cafebabe12345678"),
        patch.object(_build_info, "__commit_message__", "feat: big feature (#99)"),
        patch.object(_build_info, "__pr__", "99"),
    ):
        result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "· PR #99" in result.output
    assert "figmaclaw 2.0.0 (cafebabe · PR #99)" in result.output


def test_version_shows_unknown_when_commit_empty() -> None:
    """INVARIANT: When __commit__ is empty, sha displays as 'unknown'."""
    runner = CliRunner()
    with (
        patch.object(_build_info, "__version__", "0.1.0"),
        patch.object(_build_info, "__commit__", ""),
        patch.object(_build_info, "__commit_message__", ""),
        patch.object(_build_info, "__pr__", None),
    ):
        result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "unknown" in result.output


def test_version_no_second_line_when_message_empty() -> None:
    """INVARIANT: No second output line when commit message is empty."""
    runner = CliRunner()
    with (
        patch.object(_build_info, "__version__", "0.1.0"),
        patch.object(_build_info, "__commit__", "abc1234567890"),
        patch.object(_build_info, "__commit_message__", ""),
        patch.object(_build_info, "__pr__", None),
    ):
        result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert (
        result.output.strip().count("\n") == 0
    ), "should be exactly one line when message is empty"


def test_version_no_pr_suffix_when_pr_is_none() -> None:
    """INVARIANT: No PR suffix in the header when __pr__ is None."""
    runner = CliRunner()
    with (
        patch.object(_build_info, "__version__", "1.0.0"),
        patch.object(_build_info, "__commit__", "abc12345"),
        patch.object(_build_info, "__commit_message__", "chore: bump"),
        patch.object(_build_info, "__pr__", None),
    ):
        result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "PR" not in result.output
    assert "·" not in result.output
