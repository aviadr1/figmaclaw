"""Adversarial idempotency regressions for the "variables unavailable" path.

Most CI installations don't yet have a Figma token with the Enterprise
``file_variables:read`` scope, and they may not have a persistent
``FIGMA_MCP_TOKEN`` either. In that deployment, ``figmaclaw variables`` falls
through to ``mark_local_variables_unavailable`` for every tracked file. The
canon W-1 / TC-8 invariant says repeating the *same* unavailable verdict
must not produce a write, must not bump ``catalog.updated_at``, and must not
produce a git diff.

# Hypothesis (the bug we are pinning):
#   mark_local_variables_unavailable rewrites the per-library `fetched_at`
#   field on every call, even when the source verdict and file version are
#   unchanged. Although save_catalog uses write_json_if_changed with
#   ``ignore_keys={"updated_at", "fetched_at"}``, those keys are only
#   stripped at the *top level* — the nested per-library entries cause the
#   diff to fire and the catalog to be rewritten on every CI tick.
#
# Status (before fix): each test below FAILS. Concretely:
#   * the file mtime/content changes between two consecutive variables runs;
#   * write_json_if_changed reports True the second time;
#   * the standalone ``figmaclaw variables`` command produces a non-empty
#     COMMIT_MSG output on the second run.
#
# Result (after fix in this PR): each test PASSES. The fix lives in
# ``token_catalog.mark_local_variables_unavailable`` (skip the rewrite when
# the existing library entry is already (source=unavailable,
# source_version=current)) and in ``figma_utils.write_json_if_changed``
# (recursive ``ignore_keys`` stripping, so that any future nested timestamp
# field is also handled correctly).

The token-rotation upgrade path is also pinned: when the upstream verdict
changes from "unavailable" to a real ``LocalVariablesResponse``, the catalog
DOES get rewritten and the library source becomes ``figma_api`` —
short-circuiting must never block a real upgrade.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from figmaclaw.figma_api_models import LocalVariablesResponse
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.figma_utils import write_json_if_changed
from figmaclaw.main import cli
from figmaclaw.token_catalog import (
    TokenCatalog,
    load_catalog,
    mark_local_variables_unavailable,
    save_catalog,
)


class _SteppingClock:
    """A datetime stand-in that advances by ``step`` seconds per ``now()``.

    Used to make idempotency tests strictly deterministic — without this,
    two consecutive calls within the same pytest invocation often land in
    the same wall-clock second and the timestamp-strip comparison would
    accidentally compare equal even on the buggy code path.
    """

    UTC = datetime.UTC

    def __init__(self, start: datetime.datetime, step_seconds: int = 3600) -> None:
        self._cur = start
        self._step = datetime.timedelta(seconds=step_seconds)

    def now(self, tz: datetime.tzinfo | None = None) -> datetime.datetime:
        value = self._cur
        self._cur = self._cur + self._step
        if tz is None:
            return value.replace(tzinfo=None)
        return value.astimezone(tz)


@pytest.fixture
def stepping_clock(monkeypatch) -> Iterator[_SteppingClock]:
    clock = _SteppingClock(datetime.datetime(2026, 4, 29, 0, 0, 0, tzinfo=datetime.UTC))

    class _ProxyDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime.datetime:  # type: ignore[override]
            return clock.now(tz)

    monkeypatch.setattr("figmaclaw.token_catalog.datetime.datetime", _ProxyDatetime)
    yield clock


DS_FILE_KEY = "AZswXfXwfx2fff3RFBMo8h"
DS_FILE_NAME = "❖ Design System"
FILE_VERSION = "2347065942124128936"


def _track(repo_dir: Path) -> None:
    state = FigmaSyncState(repo_dir)
    state.add_tracked_file(DS_FILE_KEY, DS_FILE_NAME)
    state.set_file_meta(
        DS_FILE_KEY,
        version=FILE_VERSION,
        last_modified="2026-04-27T09:43:13Z",
        last_checked_at="2026-04-29T01:00:00Z",
        file_name=DS_FILE_NAME,
    )
    state.save()


def _write_enterprise_config(repo_dir: Path) -> None:
    (repo_dir / "pyproject.toml").write_text(
        '[tool.figmaclaw]\nlicense_type = "enterprise"\n',
        encoding="utf-8",
    )


class _Unavailable403Client:
    """FigmaClient stand-in: file_meta works, /variables/local returns 403."""

    def __init__(self) -> None:
        self.meta = SimpleNamespace(
            name=DS_FILE_NAME,
            version=FILE_VERSION,
            lastModified="2026-04-27T09:43:13Z",
        )
        self.calls: list[str] = []

    async def __aenter__(self) -> _Unavailable403Client:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_file_meta(self, _file_key: str) -> SimpleNamespace:
        self.calls.append("meta")
        return self.meta

    async def get_local_variables(self, _file_key: str) -> LocalVariablesResponse | None:
        self.calls.append("variables")
        return None  # 403 → caller marks unavailable


def _run_variables(repo_dir: Path, monkeypatch, client: _Unavailable403Client):
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")
    with patch("figmaclaw.commands.variables.FigmaClient", return_value=client):
        return CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(repo_dir),
                "variables",
                "--file-key",
                DS_FILE_KEY,
                "--source",
                "rest",
            ],
            catch_exceptions=False,
        )


def test_mark_unavailable_idempotent_when_version_unchanged(
    tmp_path: Path, stepping_clock: _SteppingClock
) -> None:
    """Direct call: a second mark_unavailable for the same file/version is a no-op write.

    The stepping_clock fixture forces the second call to use a strictly later
    timestamp, so a passing test cannot rely on timestamp collisions.
    """
    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog = TokenCatalog()
    mark_local_variables_unavailable(
        catalog,
        file_key=DS_FILE_KEY,
        file_name=DS_FILE_NAME,
        file_version=FILE_VERSION,
    )
    save_catalog(catalog, tmp_path)
    assert catalog_path.exists()
    text_after_first = catalog_path.read_text(encoding="utf-8")
    mtime_after_first = catalog_path.stat().st_mtime_ns

    # second pass — same input
    catalog2 = load_catalog(tmp_path)
    mark_local_variables_unavailable(
        catalog2,
        file_key=DS_FILE_KEY,
        file_name=DS_FILE_NAME,
        file_version=FILE_VERSION,
    )
    save_catalog(catalog2, tmp_path)
    text_after_second = catalog_path.read_text(encoding="utf-8")
    mtime_after_second = catalog_path.stat().st_mtime_ns

    assert text_after_second == text_after_first, (
        "mark_local_variables_unavailable wrote a phantom diff on a no-op "
        "second call. Without this guard, every CI run produces a churn commit."
    )
    assert mtime_after_second == mtime_after_first


def test_variables_command_idempotent_under_unavailable(
    tmp_path: Path, monkeypatch, stepping_clock: _SteppingClock
) -> None:
    """``figmaclaw variables`` twice on a 403 file = no second-run diff or commit.

    Stepping clock guarantees the second invocation observes a different
    wall-clock time so a timestamp-only diff cannot hide the bug behind a
    same-second collision.
    """
    _track(tmp_path)
    _write_enterprise_config(tmp_path)

    client_a = _Unavailable403Client()
    first = _run_variables(tmp_path, monkeypatch, client_a)
    assert first.exit_code == 0
    assert "kept unavailable catalog marker current" in first.output

    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    text_after_first = catalog_path.read_text(encoding="utf-8")

    client_b = _Unavailable403Client()
    second = _run_variables(tmp_path, monkeypatch, client_b)
    assert second.exit_code == 0
    text_after_second = catalog_path.read_text(encoding="utf-8")

    assert text_after_second == text_after_first, (
        "Second variables run produced a non-trivial diff on an unchanged 403 "
        "file. Canon W-1: writers must compare load-bearing content (timestamps "
        "stripped) before writing; idempotent on a no-op input."
    )
    assert "COMMIT_MSG:" not in second.output, (
        "Second run announced a commit despite no real change — "
        "this is what was producing the churn commits in CI."
    )


def test_variables_upgrade_path_still_works(tmp_path: Path, monkeypatch) -> None:
    """If the upstream answer changes from unavailable→authoritative, the
    catalog must rewrite — short-circuiting must not block real upgrades."""
    _track(tmp_path)
    _write_enterprise_config(tmp_path)

    # First: unavailable response.
    client_403 = _Unavailable403Client()
    _run_variables(tmp_path, monkeypatch, client_403)
    catalog_after_403 = load_catalog(tmp_path)
    assert catalog_after_403.libraries
    lib_403 = next(iter(catalog_after_403.libraries.values()))
    assert lib_403.source == "unavailable"

    # Now: token rotated, response is authoritative.
    class _AuthClient(_Unavailable403Client):
        async def get_local_variables(self, _file_key: str) -> LocalVariablesResponse:
            return LocalVariablesResponse.model_validate(
                {
                    "status": 200,
                    "error": False,
                    "meta": {
                        "variables": {
                            "VariableID:dsLib/1:1": {
                                "id": "VariableID:dsLib/1:1",
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
                                "variableIds": ["VariableID:dsLib/1:1"],
                            }
                        },
                    },
                }
            )

    upgrade = _run_variables(tmp_path, monkeypatch, _AuthClient())
    assert upgrade.exit_code == 0
    assert "refreshed 1 variable(s) via figma_api" in upgrade.output, upgrade.output

    catalog_after_upgrade = load_catalog(tmp_path)
    sources = {lib.source for lib in catalog_after_upgrade.libraries.values()}
    assert "figma_api" in sources, (
        "Variables upgrade from 'unavailable' to authoritative was blocked. "
        "Idempotency guards must not prevent real source upgrades."
    )


def test_write_json_if_changed_strips_nested_ignore_keys(tmp_path: Path) -> None:
    """``write_json_if_changed`` must ignore nested timestamp keys, not just
    the top level. The catalog has per-library ``fetched_at``; if those count
    as content changes, every variables run produces a phantom write."""
    path = tmp_path / "doc.json"
    payload = {
        "schema_version": 2,
        "updated_at": "2026-04-29T00:00:00Z",
        "libraries": {
            "lib-A": {
                "name": "Design System",
                "source": "unavailable",
                "fetched_at": "2026-04-29T00:00:00Z",
            }
        },
    }
    assert write_json_if_changed(path, payload, ignore_keys=frozenset({"updated_at", "fetched_at"}))

    payload_with_new_timestamps = {
        "schema_version": 2,
        "updated_at": "2026-04-29T01:00:00Z",  # top-level changed
        "libraries": {
            "lib-A": {
                "name": "Design System",
                "source": "unavailable",
                "fetched_at": "2026-04-29T01:00:00Z",  # nested changed
            }
        },
    }
    wrote = write_json_if_changed(
        path, payload_with_new_timestamps, ignore_keys=frozenset({"updated_at", "fetched_at"})
    )
    assert wrote is False, (
        "write_json_if_changed wrote a file when only ignored keys (top-level "
        "AND nested) differed. This is the underlying mechanism that turned a "
        "no-op variables run into a phantom CI commit."
    )


def test_legacy_unavailable_entry_already_in_catalog_is_idempotent(tmp_path: Path) -> None:
    """A catalog already populated with an ``unavailable`` library entry from a
    prior run does not get rewritten when we call mark_unavailable a second
    time with the same (file_key, file_version)."""
    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-existing catalog: simulate a prior run wrote this an hour ago.
    pre_existing = {
        "schema_version": 2,
        "updated_at": "2026-04-29T00:00:00Z",
        "libraries": {
            f"local:{DS_FILE_KEY}": {
                "name": DS_FILE_NAME,
                "source_file_key": DS_FILE_KEY,
                "fetched_at": "2026-04-29T00:00:00Z",
                "source_version": FILE_VERSION,
                "source": "unavailable",
                "modes": {},
                "default_mode_id": None,
                "collections": {},
            }
        },
        "variables": {},
    }
    catalog_path.write_text(json.dumps(pre_existing, indent=2), encoding="utf-8")
    text_before = catalog_path.read_text(encoding="utf-8")

    catalog = load_catalog(tmp_path)
    mark_local_variables_unavailable(
        catalog,
        file_key=DS_FILE_KEY,
        file_name=DS_FILE_NAME,
        file_version=FILE_VERSION,
    )
    save_catalog(catalog, tmp_path)

    text_after = catalog_path.read_text(encoding="utf-8")
    assert text_after == text_before, (
        "Catalog with existing unavailable entry was rewritten on no-op "
        "mark_unavailable call. This is what produced ~50 phantom commits "
        "per nightly sync run on the test/figmaclaw-pr-129-ci branch."
    )
