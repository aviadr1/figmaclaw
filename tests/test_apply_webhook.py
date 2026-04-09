"""Tests for commands/apply_webhook.py.

INVARIANTS:
- apply_webhook reads FIGMA_WEBHOOK_PAYLOAD env var and parses it as JSON
- apply_webhook skips files not in tracked_files
- apply_webhook calls pull_file for the file_id in the payload
- apply_webhook emits COMMIT_MSG: to stdout when pages were written
- apply_webhook validates the passcode when FIGMA_WEBHOOK_SECRET is set
- apply_webhook rejects payloads with wrong passcode
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.pull_logic import PullResult


def _make_payload(file_id: str = "abc123", passcode: str = "secret") -> str:
    return json.dumps(
        {
            "event_type": "FILE_UPDATE",
            "file_id": file_id,
            "passcode": passcode,
        }
    )


@pytest.mark.asyncio
async def test_apply_webhook_skips_untracked_file(tmp_path: Path, capsys):
    """INVARIANT: apply_webhook skips files not in tracked_files and emits nothing to COMMIT_MSG."""
    state = FigmaSyncState(tmp_path)
    state.load()
    # Don't add "abc123" to tracked files

    from figmaclaw.commands.apply_webhook import _run

    await _run(
        api_key="fake_key",
        repo_dir=tmp_path,
        payload=_make_payload("abc123"),
        webhook_secret=None,
    )

    out = capsys.readouterr().out
    assert "COMMIT_MSG:" not in out


@pytest.mark.asyncio
async def test_apply_webhook_calls_pull_for_tracked_file(tmp_path: Path, capsys):
    """INVARIANT: apply_webhook calls pull for a tracked file and emits COMMIT_MSG when pages written."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.save()

    from figmaclaw.commands.apply_webhook import _run

    mock_result = PullResult(file_key="abc123", pages_written=2, md_paths=["a.md", "b.md"])

    with (
        patch("figmaclaw.commands.apply_webhook.pull_file", return_value=mock_result),
        patch("figmaclaw.commands.apply_webhook.FigmaClient") as MockClient,
    ):
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_cm

        await _run(
            api_key="fake_key",
            repo_dir=tmp_path,
            payload=_make_payload("abc123"),
            webhook_secret=None,
        )

    out = capsys.readouterr().out
    assert "COMMIT_MSG:" in out
    assert "2" in out  # pages written count


@pytest.mark.asyncio
async def test_apply_webhook_rejects_wrong_passcode(tmp_path: Path):
    """INVARIANT: apply_webhook raises when passcode doesn't match FIGMA_WEBHOOK_SECRET."""
    from figmaclaw.commands.apply_webhook import WebhookAuthError, _run

    with pytest.raises(WebhookAuthError):
        await _run(
            api_key="fake_key",
            repo_dir=tmp_path,
            payload=_make_payload("abc123", passcode="wrong"),
            webhook_secret="correct_secret",
        )


@pytest.mark.asyncio
async def test_apply_webhook_accepts_correct_passcode(tmp_path: Path, capsys):
    """INVARIANT: apply_webhook proceeds when passcode matches FIGMA_WEBHOOK_SECRET."""
    state = FigmaSyncState(tmp_path)
    state.load()
    # File not tracked — just checking it doesn't raise on auth
    state.save()

    from figmaclaw.commands.apply_webhook import _run

    # Should not raise — correct passcode, file just not tracked
    await _run(
        api_key="fake_key",
        repo_dir=tmp_path,
        payload=_make_payload("abc123", passcode="correct_secret"),
        webhook_secret="correct_secret",
    )


@pytest.mark.asyncio
async def test_apply_webhook_no_commit_msg_when_nothing_written(tmp_path: Path, capsys):
    """INVARIANT: No COMMIT_MSG emitted when pull writes zero pages."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.save()

    from figmaclaw.commands.apply_webhook import _run

    mock_result = PullResult(file_key="abc123", pages_written=0)

    with (
        patch("figmaclaw.commands.apply_webhook.pull_file", return_value=mock_result),
        patch("figmaclaw.commands.apply_webhook.FigmaClient") as MockClient,
    ):
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_cm

        await _run(
            api_key="fake_key",
            repo_dir=tmp_path,
            payload=_make_payload("abc123"),
            webhook_secret=None,
        )

    out = capsys.readouterr().out
    assert "COMMIT_MSG:" not in out
