"""Tests for `figmaclaw variables` (canon TC-1, TC-5, TC-6, TC-7)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from figmaclaw.figma_api_models import LocalVariablesResponse
from figmaclaw.figma_mcp import FigmaMcpError
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli

FILE_KEY = "file123"


def _track(repo_dir: Path) -> None:
    state = FigmaSyncState(repo_dir)
    state.add_tracked_file(FILE_KEY, "Design System")
    state.save()


def _track_two_files(repo_dir: Path) -> None:
    state = FigmaSyncState(repo_dir)
    state.add_tracked_file(FILE_KEY, "Design System")
    state.add_tracked_file("file456", "Product File")
    state.save()


def _meta(version: str = "v1") -> dict:
    return {
        "name": "Design System",
        "version": version,
        "lastModified": "2026-04-28T00:00:00Z",
        "document": {"id": "0:0", "name": "Document", "type": "DOCUMENT", "children": []},
    }


def _variables(version_hash: str = "libabc") -> dict:
    return {
        "status": 200,
        "error": False,
        "meta": {
            "variables": {
                f"VariableID:{version_hash}/1:1": {
                    "id": f"VariableID:{version_hash}/1:1",
                    "name": "fg/primary",
                    "key": "fg-primary",
                    "variableCollectionId": "VariableCollectionId:1:0",
                    "resolvedType": "COLOR",
                    "valuesByMode": {"1:0": {"r": 1, "g": 0, "b": 0, "a": 1}},
                    "scopes": ["ALL_FILLS"],
                    "codeSyntax": {"WEB": "fg-primary"},
                }
            },
            "variableCollections": {
                "VariableCollectionId:1:0": {
                    "id": "VariableCollectionId:1:0",
                    "name": "Primitives",
                    "modes": [{"modeId": "1:0", "name": "Light"}],
                    "defaultModeId": "1:0",
                    "variableIds": [f"VariableID:{version_hash}/1:1"],
                }
            },
        },
    }


class _FakeClient:
    def __init__(self, variables_response: LocalVariablesResponse | None, version: str = "v1"):
        self.variables_response = variables_response
        self.version = version
        self.variables_calls = 0

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_file_meta(self, _file_key: str) -> SimpleNamespace:
        name = "Product File" if _file_key == "file456" else "Design System"
        return SimpleNamespace(
            name=name,
            version=self.version,
            lastModified="2026-04-28T00:00:00Z",
        )

    async def get_local_variables(self, _file_key: str) -> LocalVariablesResponse | None:
        self.variables_calls += 1
        return self.variables_response


def test_variables_command_refreshes_catalog(tmp_path: Path, monkeypatch) -> None:
    """INVARIANT (TC-1, TC-6): variables refresh uses /variables/local and writes v2 catalog."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(LocalVariablesResponse.model_validate(_variables()))

    with patch("figmaclaw.commands.variables.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables", "--file-key", FILE_KEY],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["schema_version"] == 2
    assert data["libraries"]["libabc"]["source_version"] == "v1"
    assert data["variables"]["VariableID:libabc/1:1"]["name"] == "fg/primary"
    assert data["variables"]["VariableID:libabc/1:1"]["source"] == "figma_api"


def test_variables_command_skips_current_catalog(tmp_path: Path, monkeypatch) -> None:
    """INVARIANT (TC-7): a current source_version skips the variables endpoint."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(LocalVariablesResponse.model_validate(_variables()))
    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "libraries": {
                    "libabc": {
                        "name": "Design System",
                        "source_file_key": FILE_KEY,
                        "source_version": "v1",
                        "source": "figma_api",
                    }
                },
                "variables": {},
            }
        ),
        encoding="utf-8",
    )

    with patch("figmaclaw.commands.variables.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables", "--file-key", FILE_KEY],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "variables unchanged" in result.output
    assert fake.variables_calls == 0


def test_variables_command_marks_403_as_seeded_fallback_current(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT (D14): Enterprise-scope 403 is not fatal and records source_version."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v2")

    with patch("figmaclaw.commands.variables.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables", "--file-key", FILE_KEY],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "endpoint unavailable" in result.output
    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source_version"] == "v2"
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"


def test_variables_command_does_not_skip_current_unavailable_marker(
    tmp_path: Path, monkeypatch
) -> None:
    """A current 403 marker must not block a later MCP authoritative refresh."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v2")
    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "libraries": {
                    f"local:{FILE_KEY}": {
                        "name": "Design System",
                        "source_file_key": FILE_KEY,
                        "source_version": "v2",
                        "source": "unavailable",
                    }
                },
                "variables": {},
            }
        ),
        encoding="utf-8",
    )
    mcp_response = LocalVariablesResponse.model_validate(_variables("mcplib"))
    mcp_export = AsyncMock(return_value=mcp_response)

    with (
        patch("figmaclaw.commands.variables.FigmaClient", return_value=fake),
        patch("figmaclaw.commands.variables.get_local_variables_via_mcp", mcp_export),
    ):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables", "--file-key", FILE_KEY],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert fake.variables_calls == 1
    mcp_export.assert_awaited_once_with(FILE_KEY)
    data = json.loads(catalog_path.read_text())
    assert data["libraries"]["mcplib"]["source"] == "figma_mcp"


def test_variables_command_auto_falls_back_to_mcp_definitions(tmp_path: Path, monkeypatch) -> None:
    """INVARIANT (TC-1, D14): REST 403 can fall back to MCP definitions."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v3")
    mcp_response = LocalVariablesResponse.model_validate(_variables("mcplib"))
    mcp_export = AsyncMock(return_value=mcp_response)

    with (
        patch("figmaclaw.commands.variables.FigmaClient", return_value=fake),
        patch("figmaclaw.commands.variables.get_local_variables_via_mcp", mcp_export),
    ):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables", "--file-key", FILE_KEY],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert fake.variables_calls == 1
    mcp_export.assert_awaited_once_with(FILE_KEY)
    assert "REST variables endpoint unavailable" in result.output
    assert "via figma_mcp" in result.output
    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"]["mcplib"]["source_version"] == "v3"
    assert data["variables"]["VariableID:mcplib/1:1"]["name"] == "fg/primary"
    assert data["variables"]["VariableID:mcplib/1:1"]["source"] == "figma_mcp"


def test_variables_command_does_not_retry_missing_mcp_credentials_per_file(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT ERR-1: persistent missing credentials can be cached per run."""
    _track_two_files(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v5")
    mcp_export = AsyncMock(side_effect=FigmaMcpError("FIGMA_MCP_TOKEN missing"))

    with (
        patch("figmaclaw.commands.variables.FigmaClient", return_value=fake),
        patch("figmaclaw.commands.variables.get_local_variables_via_mcp", mcp_export),
    ):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert fake.variables_calls == 2
    mcp_export.assert_awaited_once_with(FILE_KEY)
    assert result.output.count("Figma MCP variables fallback unavailable") == 1

    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"
    assert data["libraries"]["local:file456"]["source"] == "unavailable"


def test_variables_command_retries_mcp_after_transient_file_error(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT ERR-1: a transient per-file MCP error must not poison later files."""
    _track_two_files(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v8")
    mcp_response = LocalVariablesResponse.model_validate(_variables("mcplib2"))
    mcp_export = AsyncMock(
        side_effect=[
            FigmaMcpError(
                "MCP variables export failed: Operation attempted to modify the file while in read-only mode."
            ),
            mcp_response,
        ]
    )

    with (
        patch("figmaclaw.commands.variables.FigmaClient", return_value=fake),
        patch("figmaclaw.commands.variables.get_local_variables_via_mcp", mcp_export),
    ):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert fake.variables_calls == 2
    assert mcp_export.await_count == 2
    assert "file123: Figma MCP variables fallback unavailable" in result.output
    assert "Product File: refreshed 1 variable(s) via figma_mcp" in result.output

    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"
    assert data["libraries"]["mcplib2"]["source"] == "figma_mcp"


def test_variables_command_source_mcp_skips_rest_variables(tmp_path: Path, monkeypatch) -> None:
    """Explicit MCP mode uses the plugin-runtime reader directly."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v4")
    mcp_response = LocalVariablesResponse.model_validate(_variables("mcplib"))
    mcp_export = AsyncMock(return_value=mcp_response)

    with (
        patch("figmaclaw.commands.variables.FigmaClient", return_value=fake),
        patch("figmaclaw.commands.variables.get_local_variables_via_mcp", mcp_export),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "variables",
                "--file-key",
                FILE_KEY,
                "--source",
                "mcp",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert fake.variables_calls == 0
    mcp_export.assert_awaited_once_with(FILE_KEY)
    assert "via figma_mcp" in result.output


def test_variables_command_require_authoritative_fails_on_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT AUTH-1: strict proof fails on unavailable fallback markers."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v6")

    with patch("figmaclaw.commands.variables.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "variables",
                "--file-key",
                FILE_KEY,
                "--source",
                "rest",
                "--require-authoritative",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    assert "authoritative variables missing" in result.output
    assert "library source(s): unavailable" in result.output
    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"


def test_variables_command_require_authoritative_accepts_definitions(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT AUTH-1: strict proof passes when REST or MCP produced definitions."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(LocalVariablesResponse.model_validate(_variables()), version="v7")

    with patch("figmaclaw.commands.variables.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "variables",
                "--file-key",
                FILE_KEY,
                "--require-authoritative",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "refreshed 1 variable(s) via figma_api" in result.output
