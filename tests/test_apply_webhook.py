"""Tests for commands/apply_webhook.py.

INVARIANTS:
- apply_webhook reads FIGMA_WEBHOOK_PAYLOAD env var and parses it as JSON
- apply_webhook skips files not in tracked_files
- apply_webhook calls pull_file for file_key (and supports legacy file_id)
- apply_webhook emits COMMIT_MSG: to stdout when pages were written
- apply_webhook validates the passcode when FIGMA_WEBHOOK_SECRET is set
- apply_webhook rejects payloads with wrong passcode
- apply_webhook prunes generated artifacts on FILE_DELETE
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry
from figmaclaw.pull_logic import PullResult


def _make_payload(
    file_key: str = "abc123",
    passcode: str = "secret",
    *,
    event_type: str = "FILE_UPDATE",
    use_legacy_file_id: bool = False,
) -> str:
    payload: dict[str, str] = {
        "event_type": event_type,
        "passcode": passcode,
    }
    if use_legacy_file_id:
        payload["file_id"] = file_key
    else:
        payload["file_key"] = file_key
    return json.dumps(payload)


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
async def test_apply_webhook_supports_legacy_file_id_payload(tmp_path: Path, capsys):
    """INVARIANT: apply_webhook accepts legacy payloads that send file_id instead of file_key."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.save()

    from figmaclaw.commands.apply_webhook import _run

    mock_result = PullResult(file_key="abc123", pages_written=1, md_paths=["a.md"])

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
            payload=_make_payload("abc123", use_legacy_file_id=True),
            webhook_secret=None,
        )

    out = capsys.readouterr().out
    assert "COMMIT_MSG:" in out
    assert "[abc123]" in out


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


@pytest.mark.asyncio
async def test_apply_webhook_file_delete_prunes_artifacts(tmp_path: Path, capsys):
    """INVARIANT: FILE_DELETE removes tracked generated paths and emits a commit message."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.manifest.files["abc123"].pages["11:1"] = PageEntry(
        page_name="Page",
        page_slug="page-11-1",
        md_path="figma/web-app/pages/page-11-1.md",
        page_hash="hash",
        last_refreshed_at="now",
        component_md_paths=["figma/web-app/components/comp-20-1.md"],
    )
    state.save()

    page_md = tmp_path / "figma/web-app/pages/page-11-1.md"
    page_md.parent.mkdir(parents=True, exist_ok=True)
    page_md.write_text("x")
    page_sidecar = page_md.with_suffix(".tokens.json")
    page_sidecar.write_text("{}")
    comp_md = tmp_path / "figma/web-app/components/comp-20-1.md"
    comp_md.parent.mkdir(parents=True, exist_ok=True)
    comp_md.write_text("y")

    from figmaclaw.commands.apply_webhook import _run

    await _run(
        api_key="fake_key",
        repo_dir=tmp_path,
        payload=_make_payload("abc123", event_type="FILE_DELETE"),
        webhook_secret=None,
    )

    out = capsys.readouterr().out
    assert "COMMIT_MSG:" in out
    assert "file deleted [abc123]" in out
    assert not page_md.exists()
    assert not page_sidecar.exists()
    assert not comp_md.exists()

    state2 = FigmaSyncState(tmp_path)
    state2.load()
    assert "abc123" not in state2.manifest.tracked_files
    assert "abc123" not in state2.manifest.files
