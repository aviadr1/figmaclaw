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


def _write_enterprise_config(repo_dir: Path) -> None:
    (repo_dir / "pyproject.toml").write_text(
        '[tool.figmaclaw]\nlicense_type = "enterprise"\n',
        encoding="utf-8",
    )


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


def _local_variables() -> dict:
    """Variables whose IDs have no published-library hash segment.

    These are stored under the synthetic ``local:<file_key>`` catalog library,
    which is the shape used by Tap In / LSN Branding.
    """
    return {
        "status": 200,
        "error": False,
        "meta": {
            "variables": {
                "VariableID:1:1": {
                    "id": "VariableID:1:1",
                    "name": "fg/primary",
                    "key": "fg-primary",
                    "variableCollectionId": "VariableCollectionId:1:0",
                    "resolvedType": "COLOR",
                    "valuesByMode": {
                        "light": {"r": 1, "g": 1, "b": 1, "a": 1},
                        "dark": {"r": 0, "g": 0, "b": 0, "a": 1},
                    },
                    "scopes": ["TEXT_FILL"],
                    "codeSyntax": {"WEB": "fg-primary"},
                }
            },
            "variableCollections": {
                "VariableCollectionId:1:0": {
                    "id": "VariableCollectionId:1:0",
                    "name": "Semantic",
                    "modes": [
                        {"modeId": "light", "name": "Light"},
                        {"modeId": "dark", "name": "Dark"},
                    ],
                    "defaultModeId": "light",
                    "variableIds": ["VariableID:1:1"],
                }
            },
        },
    }


class _FakeClient:
    def __init__(
        self,
        variables_response: LocalVariablesResponse | None,
        version: str = "v1",
        variables_unavailable_reason: str | None = None,
        editor_type: str = "figma",
    ):
        self.variables_response = variables_response
        self.version = version
        self.variables_unavailable_reason = variables_unavailable_reason
        self.editor_type = editor_type
        self.variables_calls = 0
        self.meta_calls = 0

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_file_meta(self, _file_key: str) -> SimpleNamespace:
        self.meta_calls += 1
        name = "Product File" if _file_key == "file456" else "Design System"
        return SimpleNamespace(
            name=name,
            version=self.version,
            lastModified="2026-04-28T00:00:00Z",
            editorType=self.editor_type,
        )

    async def get_local_variables(self, _file_key: str) -> LocalVariablesResponse | None:
        self.variables_calls += 1
        return self.variables_response

    async def get_local_variables_with_reason(
        self, _file_key: str
    ) -> tuple[LocalVariablesResponse | None, str | None]:
        self.variables_calls += 1
        return self.variables_response, self.variables_unavailable_reason

    async def list_team_projects(self, _team_id: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="proj1", name="CURRENT")]

    async def list_project_files(self, _project_id: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                key=FILE_KEY,
                name="Design System",
                last_modified="2026-04-28T00:00:00Z",
            ),
            SimpleNamespace(
                key="file456",
                name="Product File",
                last_modified="2026-04-28T00:00:00Z",
            ),
        ]


def test_variables_command_refreshes_catalog(tmp_path: Path, monkeypatch) -> None:
    """INVARIANT (TC-1, TC-6): variables refresh uses /variables/local and writes v2 catalog."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
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
    assert data["variables"]["VariableID:libabc/1:1"]["key"] == "fg-primary"
    assert data["variables"]["VariableID:libabc/1:1"]["source"] == "figma_api"


def test_variables_command_skips_current_catalog(tmp_path: Path, monkeypatch) -> None:
    """INVARIANT (TC-7): a current source_version skips the variables endpoint."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
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


def test_variables_command_listing_skips_current_catalog_without_meta_calls(
    tmp_path: Path, monkeypatch
) -> None:
    """ERR-2: no-op all-file variables runs use the listing freshness gate."""
    _track(tmp_path)
    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.files[FILE_KEY].version = "v1"
    state.manifest.files[FILE_KEY].last_modified = "2026-04-28T00:00:00Z"
    state.save()
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
                        "source": "figma_mcp",
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
            ["--repo-dir", str(tmp_path), "variables", "--team-id", "team1"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "SYNC_OBS_VARIABLES event=listing_prefilter" in result.output
    assert "listing current, version v1" in result.output
    assert fake.meta_calls == 0
    assert fake.variables_calls == 0


def test_variables_command_marks_403_as_seeded_fallback_current(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT (D14): Enterprise-scope 403 is not fatal and records source_version."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
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


def test_variables_command_auto_skips_rest_variables_without_enterprise_license(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT: non-Enterprise is the default; auto does not probe REST variables."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v2")
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
    assert fake.variables_calls == 0
    mcp_export.assert_awaited_once_with(FILE_KEY)
    assert "via figma_mcp" in result.output


def test_variables_command_source_rest_requires_enterprise_license(
    tmp_path: Path, monkeypatch
) -> None:
    """Explicit REST mode refuses Enterprise-only probes unless configured."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(LocalVariablesResponse.model_validate(_variables()))

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
            ],
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    assert "REST variables require Figma Enterprise" in result.output
    assert fake.variables_calls == 0


def test_variables_command_skips_current_unavailable_marker_during_retry_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT TC-10: default auto refresh observes unavailable retry backoff."""
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
                        "unavailable_retry_after": "2099-01-01T00:00:00Z",
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
    assert "variables unavailable unchanged" in result.output
    assert "will retry after 2099-01-01T00:00:00Z" in result.output
    assert fake.variables_calls == 0
    mcp_export.assert_not_awaited()
    data = json.loads(catalog_path.read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"


def test_variables_command_retries_current_unavailable_marker_after_cooldown(
    tmp_path: Path, monkeypatch
) -> None:
    """Expired retry deadlines self-heal instead of poisoning a file forever."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
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
                        "unavailable_retry_after": "2000-01-01T00:00:00Z",
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


def test_variables_command_retries_legacy_unavailable_marker_without_retry_after(
    tmp_path: Path, monkeypatch
) -> None:
    """Legacy unavailable markers remain retryable; no permanent poison pill."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
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


def test_variables_command_force_retries_current_unavailable_marker(
    tmp_path: Path, monkeypatch
) -> None:
    """A current unavailable marker remains retryable when the operator asks."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
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
                        "unavailable_retry_after": "2099-01-01T00:00:00Z",
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
            ["--repo-dir", str(tmp_path), "variables", "--file-key", FILE_KEY, "--force"],
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
    _write_enterprise_config(tmp_path)
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


def test_variables_command_auto_commit_batches_catalog_changes(tmp_path: Path, monkeypatch) -> None:
    """Efficiency regression: one variables run should publish one catalog snapshot.

    The catalog is a deterministic file-scope registry. When multiple tracked
    files update it in one command, per-file git commits add CI churn without
    adding provenance beyond the resulting ds_catalog.json snapshot.
    """
    _track_two_files(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v3")
    mcp_export = AsyncMock(
        side_effect=[
            LocalVariablesResponse.model_validate(_variables("mcplib1")),
            LocalVariablesResponse.model_validate(_variables("mcplib2")),
        ]
    )

    with (
        patch("figmaclaw.commands.variables.FigmaClient", return_value=fake),
        patch("figmaclaw.commands.variables.get_local_variables_via_mcp", mcp_export),
        patch("figmaclaw.commands.variables.git_commit", return_value=True) as git_commit,
    ):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables", "--auto-commit"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert git_commit.call_count == 1
    repo_dir, paths, message = git_commit.call_args.args
    assert repo_dir == tmp_path
    assert paths == [".figma-sync/ds_catalog.json"]
    assert message == "sync: figmaclaw variables — 2 file(s) updated"
    assert result.output.count("  ✓ committed") == 1
    assert "COMMIT_MSG:sync: figmaclaw variables updated" in result.output


def test_variables_command_records_source_lifecycle_from_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT TC-12: token libraries preserve source-system lifecycle."""
    _track(tmp_path)
    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.files[FILE_KEY].source_project_id = "proj-archive"
    state.manifest.files[FILE_KEY].source_project_name = "ARCHIVE"
    state.manifest.files[FILE_KEY].source_lifecycle = "archived"
    state.save()
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
    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    library = data["libraries"]["mcplib"]
    assert library["source_project_id"] == "proj-archive"
    assert library["source_project_name"] == "ARCHIVE"
    assert library["source_lifecycle"] == "archived"


def test_variables_command_updates_source_lifecycle_before_current_skip(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT TC-12: version-current catalog entries still receive source provenance."""
    _track(tmp_path)
    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.files[FILE_KEY].source_project_id = "proj-archive"
    state.manifest.files[FILE_KEY].source_project_name = "ARCHIVE"
    state.manifest.files[FILE_KEY].source_lifecycle = "archived"
    state.save()
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v1")
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
                        "source": "figma_mcp",
                    }
                },
                "variables": {},
            }
        ),
        encoding="utf-8",
    )
    mcp_export = AsyncMock()

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
    assert "variables unchanged" in result.output
    mcp_export.assert_not_awaited()
    data = json.loads(catalog_path.read_text())
    library = data["libraries"]["libabc"]
    assert library["source_project_id"] == "proj-archive"
    assert library["source_project_name"] == "ARCHIVE"
    assert library["source_lifecycle"] == "archived"


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
    assert fake.variables_calls == 0
    mcp_export.assert_awaited_once_with(FILE_KEY)
    assert result.output.count("Figma MCP variables fallback unavailable") == 1

    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"
    assert data["libraries"]["local:file456"]["source"] == "unavailable"


def test_variables_command_does_not_retry_missing_rest_scope_per_file(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT ERR-1: missing file_variables:read is cached per run."""
    _track_two_files(tmp_path)
    _write_enterprise_config(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(
        None,
        version="v5",
        variables_unavailable_reason=(
            "Invalid scope(s): file_content:read, projects:read. "
            "This endpoint requires the file_variables:read scope"
        ),
    )
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
    assert fake.variables_calls == 1
    assert mcp_export.await_count == 2
    assert result.output.count("token lacks file_variables:read") == 1
    assert "Product File: refreshed 1 variable(s) via figma_mcp" in result.output

    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"
    assert data["libraries"]["mcplib2"]["source"] == "figma_mcp"


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
    assert fake.variables_calls == 0
    assert mcp_export.await_count == 2
    assert "file123: Figma MCP variables fallback unavailable" in result.output
    assert "Product File: refreshed 1 variable(s) via figma_mcp" in result.output

    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["libraries"][f"local:{FILE_KEY}"]["source"] == "unavailable"
    assert data["libraries"]["mcplib2"]["source"] == "figma_mcp"


def test_variables_command_auto_skips_mcp_variable_export_for_figjam(
    tmp_path: Path, monkeypatch
) -> None:
    """FigJam files do not use the Figma Design local-variable registry path."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v9", editor_type="figjam")
    mcp_export = AsyncMock()

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
    assert fake.variables_calls == 0
    mcp_export.assert_not_awaited()
    assert "variables registry unavailable for editorType='figjam'" in result.output

    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    library = data["libraries"][f"local:{FILE_KEY}"]
    assert library["source"] == "unavailable"
    assert library["source_version"] == "v9"
    assert "unavailable_retry_after" in library


def test_variables_command_enterprise_auto_skips_figjam_variable_registry(
    tmp_path: Path, monkeypatch
) -> None:
    """Enterprise REST config does not make FigJam a Design variable registry."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v9", editor_type="figjam")
    mcp_export = AsyncMock()

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
    assert fake.variables_calls == 0
    mcp_export.assert_not_awaited()
    assert "variables registry unavailable for editorType='figjam'" in result.output


def test_variables_command_source_mcp_rejects_figjam_before_use_figma(
    tmp_path: Path, monkeypatch
) -> None:
    """Explicit MCP mode fails fast instead of sending read JS through use_figma."""
    _track(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    fake = _FakeClient(None, version="v9", editor_type="figjam")
    mcp_export = AsyncMock()

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

    assert result.exit_code != 0
    assert "unsupported for FigJam files" in result.output
    mcp_export.assert_not_awaited()


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


def test_variables_command_emits_reader_observability(tmp_path: Path, monkeypatch) -> None:
    """ERR-2: variables refresh identifies REST-vs-MCP time and file scope."""
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
            ["--repo-dir", str(tmp_path), "variables", "--file-key", FILE_KEY],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "SYNC_OBS_VARIABLES event=run_start" in result.output
    assert f"SYNC_OBS_VARIABLES event=file_start file_key={FILE_KEY}" in result.output
    assert f"SYNC_OBS_VARIABLES event=meta_start file_key={FILE_KEY}" in result.output
    assert "reader=rest reason=non_enterprise_license" in result.output
    assert f"SYNC_OBS_VARIABLES event=reader_start file_key={FILE_KEY} reader=mcp" in result.output
    assert "SYNC_OBS_VARIABLES event=reader_end" in result.output
    assert "reader=mcp outcome=definitions" in result.output
    assert "SYNC_OBS_VARIABLES event=file_end" in result.output
    assert "outcome=refreshed" in result.output
    assert "SYNC_OBS_VARIABLES event=run_end" in result.output


def test_variables_command_unavailable_does_not_downgrade_local_mcp_definitions(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT AUTH-1/TC-7: unavailable probes do not clobber local MCP libraries.

    Tap In and LSN variables are local to their files, so their catalog library
    key is ``local:<file_key>``. A later unavailable REST/MCP probe must not
    overwrite that same key with ``source=unavailable`` and lose modes,
    collections, or default-mode metadata.
    """
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    mcp_response = LocalVariablesResponse.model_validate(_local_variables())
    mcp_export = AsyncMock(return_value=mcp_response)

    with (
        patch("figmaclaw.commands.variables.FigmaClient", return_value=_FakeClient(None, "v1")),
        patch("figmaclaw.commands.variables.get_local_variables_via_mcp", mcp_export),
    ):
        first = CliRunner().invoke(
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

    assert first.exit_code == 0
    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    before = json.loads(catalog_path.read_text())
    assert before["libraries"][f"local:{FILE_KEY}"]["source"] == "figma_mcp"
    assert before["libraries"][f"local:{FILE_KEY}"]["default_mode_id"] == "light"
    assert before["variables"]["VariableID:1:1"]["source"] == "figma_mcp"

    with patch("figmaclaw.commands.variables.FigmaClient", return_value=_FakeClient(None, "v2")):
        second = CliRunner().invoke(
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

    assert second.exit_code != 0
    assert "preserved authoritative catalog from version(s): v1" in second.output
    assert "authoritative variables registry is stale" in second.output
    after = json.loads(catalog_path.read_text())
    assert after["libraries"][f"local:{FILE_KEY}"]["source"] == "figma_mcp"
    assert after["libraries"][f"local:{FILE_KEY}"]["default_mode_id"] == "light"
    assert after["libraries"][f"local:{FILE_KEY}"]["modes"] == {
        "light": "Light",
        "dark": "Dark",
    }
    assert after["variables"]["VariableID:1:1"]["name"] == "fg/primary"


def test_variables_command_require_authoritative_fails_on_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """INVARIANT AUTH-1: strict proof fails on unavailable fallback markers."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)
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
    _write_enterprise_config(tmp_path)
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
