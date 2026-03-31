"""Tests for commands/list_files.py.

INVARIANTS:
- list_cmd requires FIGMA_API_KEY to be set
- list_cmd calls list_team_projects then list_project_files per project
- list_cmd filters files by last_modified when --since is given
- list_cmd marks already-tracked files with [tracked]
- list_cmd exits with a message when no files match the filter
- list_cmd rejects invalid --since values
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli


TEAM_ID = "1314617533998771588"
FILE_KEY_A = "aaaabbbbcccc"
FILE_KEY_B = "ddddeeeefffg"


def _project(project_id: str = "proj1", name: str = "Web") -> dict:
    return {"id": project_id, "name": name}


def _file(key: str, name: str, last_modified: str = "2026-01-15T10:00:00Z") -> dict:
    return {"key": key, "name": name, "last_modified": last_modified}


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def mock_client():
    client = MagicMock(spec=FigmaClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.list_team_projects = AsyncMock(return_value=[_project()])
    client.list_project_files = AsyncMock(return_value=[
        _file(FILE_KEY_A, "Web App", "2026-03-01T00:00:00Z"),
        _file(FILE_KEY_B, "Mobile App", "2025-10-01T00:00:00Z"),
    ])
    return client


def test_list_cmd_requires_figma_api_key(runner: CliRunner, tmp_path: Path):
    """INVARIANT: list exits with an error if FIGMA_API_KEY is not set."""
    env = {k: v for k, v in os.environ.items() if k != "FIGMA_API_KEY"}
    result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "list", TEAM_ID], env=env)
    assert result.exit_code != 0
    assert "FIGMA_API_KEY" in result.output


def test_list_cmd_shows_all_files(runner: CliRunner, tmp_path: Path, mock_client):
    """INVARIANT: Without --since, all files from every project are shown."""
    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "list", TEAM_ID],
            env={"FIGMA_API_KEY": "figd_test"},
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert FILE_KEY_A in result.output
    assert FILE_KEY_B in result.output
    assert "Web App" in result.output
    assert "Mobile App" in result.output


def test_list_cmd_filters_by_since(runner: CliRunner, tmp_path: Path, mock_client):
    """INVARIANT: --since 3m excludes files older than 3 months."""
    # FILE_KEY_A last modified 2026-03-01 (recent); FILE_KEY_B 2025-10-01 (old)
    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "list", TEAM_ID, "--since", "3m"],
            env={"FIGMA_API_KEY": "figd_test"},
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert FILE_KEY_A in result.output
    assert FILE_KEY_B not in result.output


def test_list_cmd_marks_tracked_files(runner: CliRunner, tmp_path: Path, mock_client):
    """INVARIANT: Files already in the manifest are labelled [tracked]."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(FILE_KEY_A, "Web App")
    state.save()

    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "list", TEAM_ID],
            env={"FIGMA_API_KEY": "figd_test"},
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "[tracked]" in result.output
    # Only the tracked file should be labelled
    lines_with_tracked = [l for l in result.output.splitlines() if "[tracked]" in l]
    assert len(lines_with_tracked) == 1
    assert FILE_KEY_A in lines_with_tracked[0]


def test_list_cmd_no_results_message(runner: CliRunner, tmp_path: Path):
    """INVARIANT: If no files match the filter, a helpful message is shown."""
    client = MagicMock(spec=FigmaClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.list_team_projects = AsyncMock(return_value=[_project()])
    # All files are old
    client.list_project_files = AsyncMock(return_value=[
        _file(FILE_KEY_A, "Old App", "2020-01-01T00:00:00Z"),
    ])

    with patch.object(FigmaClient, "__new__", return_value=client):
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "list", TEAM_ID, "--since", "3m"],
            env={"FIGMA_API_KEY": "figd_test"},
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "No files found" in result.output


def test_list_cmd_rejects_invalid_since(runner: CliRunner, tmp_path: Path):
    """INVARIANT: An unrecognised --since value exits with a usage error."""
    result = runner.invoke(
        cli,
        ["--repo-dir", str(tmp_path), "list", TEAM_ID, "--since", "badvalue"],
        env={"FIGMA_API_KEY": "figd_test"},
    )
    assert result.exit_code != 0
    assert "Cannot parse" in result.output or "Error" in result.output


def test_list_cmd_accepts_team_url(runner: CliRunner, tmp_path: Path, mock_client):
    """INVARIANT: A full Figma team URL is accepted (team ID extracted automatically)."""
    team_url = f"https://www.figma.com/files/team/{TEAM_ID}/Gigaverse"
    with patch.object(FigmaClient, "__new__", return_value=mock_client):
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "list", team_url],
            env={"FIGMA_API_KEY": "figd_test"},
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    # list_team_projects should have been called with the extracted numeric ID
    mock_client.list_team_projects.assert_called_once_with(TEAM_ID)
