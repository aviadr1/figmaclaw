"""Consumer-contract regressions distilled from the real linear-git repo.

These tests intentionally use small fixtures with the same *shape* as the
current linear-git state:

- legacy schema-v1 ``.figma-sync/ds_catalog.json`` with only observed
  ``VariableID:*`` entries plus CSS-seeded ``SEEDED:*`` bridge entries;
- a tracked Tap In Design System file whose variables must be refreshed by the
  new file-scope variables workflow;
- token sidecars that must not receive suggestions from observation-only
  catalog data.

They are not live Figma tests. The point is to pin the consumer repo contract
that previously let partial, stale, or non-authoritative token data look good
enough to keep CI doing repeated work.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from figmaclaw.figma_api_models import LocalVariablesResponse
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli
from figmaclaw.token_catalog import DEFAULT_MODE_ID, catalog_staleness_errors, load_catalog

TAP_IN_DS_FILE_KEY = "dcDETwKMNGpK39FfApg7Ki"


def _write_legacy_linear_git_catalog(repo_dir: Path) -> None:
    """Write a minimal schema-v1 catalog matching the current linear-git shape."""
    path = repo_dir / ".figma-sync" / "ds_catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": "2026-04-28T12:00:00Z",
                "variables": {
                    "SEEDED:font-size-ds-md": {
                        "name": "font-size/md",
                        "hex": None,
                        "numeric_value": 16.0,
                        "observed_on": ["fontSize"],
                    },
                    "VariableID:legacyLib/12:34": {
                        "name": None,
                        "hex": "#FFFFFF",
                        "numeric_value": None,
                        "observed_on": ["fill"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _track_tap_in_ds(repo_dir: Path, *, version: str = "2347540107596801534") -> None:
    state = FigmaSyncState(repo_dir)
    state.add_tracked_file(TAP_IN_DS_FILE_KEY, "TAP IN DESIGN SYSTEM")
    state.set_file_meta(
        TAP_IN_DS_FILE_KEY,
        version=version,
        last_modified="2026-04-28T16:10:25Z",
        last_checked_at="2026-04-28T16:11:00Z",
        file_name="TAP IN DESIGN SYSTEM",
    )
    state.save()


def _local_variables_response() -> dict:
    return {
        "status": 200,
        "error": False,
        "meta": {
            "variables": {
                "VariableID:tapInLib/1:1": {
                    "id": "VariableID:tapInLib/1:1",
                    "name": "fg/primary",
                    "key": "fg-primary",
                    "variableCollectionId": "VariableCollectionId:1:0",
                    "resolvedType": "COLOR",
                    "valuesByMode": {"1:0": {"r": 0.1, "g": 0.2, "b": 0.3, "a": 1}},
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
                    "variableIds": ["VariableID:tapInLib/1:1"],
                }
            },
        },
    }


class _FakeVariablesClient:
    def __init__(self) -> None:
        self.meta = SimpleNamespace(
            name="TAP IN DESIGN SYSTEM",
            version="2347540107596801534",
            lastModified="2026-04-28T16:10:25Z",
        )

    async def __aenter__(self) -> _FakeVariablesClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_file_meta(self, _file_key: str) -> SimpleNamespace:
        return self.meta

    async def get_local_variables(self, _file_key: str) -> LocalVariablesResponse:
        return LocalVariablesResponse.model_validate(_local_variables_response())


def test_linear_git_legacy_catalog_migrates_without_dropping_bridge_entries(
    tmp_path: Path,
) -> None:
    """INVARIANT: schema-v1 linear-git catalogs migrate, preserving all evidence.

    The current consumer catalog has many observation-only ``VariableID:*``
    entries and a small number of named ``SEEDED:*`` CSS bridge entries. Loading
    that catalog must not drop either class, but it also must not pretend the
    observed entries are authoritative variable definitions.
    """
    _write_legacy_linear_git_catalog(tmp_path)

    catalog = load_catalog(tmp_path)

    assert catalog.schema_version == 2
    assert catalog.libraries == {}

    seeded = catalog.variables["SEEDED:font-size-ds-md"]
    assert seeded.name == "font-size/md"
    assert seeded.source == "seeded:legacy"
    assert seeded.values_by_mode[DEFAULT_MODE_ID].numeric_value == 16.0
    assert seeded.observed_on == ["fontSize"]

    observed = catalog.variables["VariableID:legacyLib/12:34"]
    assert observed.library_hash == "legacyLib"
    assert observed.source == "observed"
    assert observed.values_by_mode[DEFAULT_MODE_ID].hex == "#FFFFFF"
    assert observed.observed_on == ["fill"]


def test_suggest_tokens_refuses_migrated_observation_only_linear_git_catalog(
    tmp_path: Path,
) -> None:
    """INVARIANT: migrated legacy observations cannot drive token suggestions.

    Without a current file-scope variables registry, the safe result is a hard
    stop telling CI to run ``figmaclaw variables``. Returning ``no_match`` from
    stale observation-only data would hide missing DS tokens and create repeated
    audit work.
    """
    _track_tap_in_ds(tmp_path)
    _write_legacy_linear_git_catalog(tmp_path)

    catalog = load_catalog(tmp_path)
    state = FigmaSyncState(tmp_path)
    state.load()
    assert catalog_staleness_errors(catalog, state, TAP_IN_DS_FILE_KEY)

    sidecar = tmp_path / "page.tokens.json"
    sidecar.write_text(
        json.dumps(
            {
                "file_key": TAP_IN_DS_FILE_KEY,
                "frames": {"1:1": {"issues": [{"property": "fill", "hex": "#FFFFFF"}]}},
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["--repo-dir", str(tmp_path), "suggest-tokens", "--sidecar", str(sidecar)],
    )

    assert result.exit_code != 0
    assert "ds_catalog.json has no variables registry" in result.output
    assert f"figmaclaw variables --file-key {TAP_IN_DS_FILE_KEY}" in result.output


def test_variables_refresh_upgrades_legacy_linear_git_catalog_without_touching_pages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """INVARIANT: variables refresh is file-scope, migratory, and non-destructive."""
    _track_tap_in_ds(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.figmaclaw]\nlicense_type = "enterprise"\n',
        encoding="utf-8",
    )
    _write_legacy_linear_git_catalog(tmp_path)
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")

    with patch("figmaclaw.commands.variables.FigmaClient", return_value=_FakeVariablesClient()):
        result = CliRunner().invoke(
            cli,
            ["--repo-dir", str(tmp_path), "variables", "--file-key", TAP_IN_DS_FILE_KEY],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert "refreshed 1 variable(s) via figma_api" in result.output

    data = json.loads((tmp_path / ".figma-sync" / "ds_catalog.json").read_text())
    assert data["schema_version"] == 2
    assert data["libraries"]["tapInLib"]["source_file_key"] == TAP_IN_DS_FILE_KEY
    assert data["libraries"]["tapInLib"]["source_version"] == "2347540107596801534"
    assert data["libraries"]["tapInLib"]["source"] == "figma_api"

    assert data["variables"]["SEEDED:font-size-ds-md"]["source"] == "seeded:legacy"
    assert data["variables"]["VariableID:legacyLib/12:34"]["source"] == "observed"
    assert data["variables"]["VariableID:tapInLib/1:1"]["name"] == "fg/primary"
    assert data["variables"]["VariableID:tapInLib/1:1"]["source"] == "figma_api"

    assert not (tmp_path / "figma").exists(), "variables refresh must not create page artifacts"
