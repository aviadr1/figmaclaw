from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli

FILE_KEY = "file123"
PAGE_ID = "10:1"
VAR_ID = "VariableID:libabc/1:1"


def _track_current(repo_dir: Path, *, file_key: str = FILE_KEY, version: str = "v2") -> None:
    state = FigmaSyncState(repo_dir)
    state.add_tracked_file(file_key, "Design System")
    state.set_file_meta(
        file_key,
        version=version,
        last_modified="2026-05-08T00:00:00Z",
        last_checked_at="2026-05-08T00:00:00Z",
        file_name="Design System",
    )
    state.save()


def _write_catalog(
    repo_dir: Path,
    *,
    source_version: str = "v2",
    source: str = "figma_api",
    key: str | None = "fg-primary-key",
    library_hash: str = "libabc",
    name: str = "fg/primary",
    source_file_key: str = FILE_KEY,
) -> None:
    catalog_path = repo_dir / ".figma-sync" / "ds_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    variable = {
        "library_hash": library_hash,
        "collection_id": "collection1",
        "name": name,
        "resolved_type": "COLOR",
        "values_by_mode": {"light": {"hex": "#FFFFFF"}},
        "source": source,
    }
    if key is not None:
        variable["key"] = key
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "libraries": {
                    library_hash: {
                        "name": "TapIn Design System",
                        "source_file_key": source_file_key,
                        "source_version": source_version,
                        "source": source,
                    }
                },
                "variables": {VAR_ID: variable},
            }
        ),
        encoding="utf-8",
    )


def test_apply_tokens_dry_run_resolves_compact_rows_against_authoritative_catalog(
    tmp_path: Path,
) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary", "v": "#FFFFFF"}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["mode"] == "dry-run"
    assert data["ok"] is True
    assert data["fixes"] == 1
    assert data["refusals"] == 0


def test_apply_tokens_stale_gate_checks_referenced_catalog_source_not_apply_file(
    tmp_path: Path,
) -> None:
    _track_current(tmp_path, file_key="ds-file", version="v2")
    _write_catalog(tmp_path, source_file_key="ds-file", source_version="v2")
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary", "v": "#FFFFFF"}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            "consumer-file",
            "--page",
            PAGE_ID,
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["ok"] is True


def test_apply_tokens_emit_only_writes_deterministic_batches(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(
        json.dumps(
            [
                {"n": "1:2", "p": "fill", "t": "fg/primary", "v": "#FFFFFF"},
                {"n": "1:3", "p": "cornerRadius", "t": "fg/primary", "v": 4},
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--emit-only",
            "--batch-size",
            "1",
            "--batch-dir",
            "apply_batches",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    batch_dir = tmp_path / "apply_batches"
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["total_fixes"] == 2
    assert manifest["batch_count"] == 2
    first_batch = json.loads((batch_dir / "batch-0001.json").read_text(encoding="utf-8"))
    assert first_batch == [
        {
            "node_id": "1:2",
            "paint_index": 0,
            "property": "fill",
            "token_name": "fg/primary",
            "value": "#FFFFFF",
            "variable_id": VAR_ID,
            "variable_key": "fg-primary-key",
        }
    ]
    js = (batch_dir / "batch-0001.use_figma.js").read_text(encoding="utf-8")
    assert "Generated by figmaclaw apply-tokens" in js
    assert 'const TARGET_PAGE_ID = "10:1";' in js
    assert "importVariableByKeyAsync" in js
    assert "apply-tokens batch incomplete" in js


def test_apply_tokens_emit_only_removes_stale_generated_batches(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(
        json.dumps(
            [
                {"n": "1:2", "p": "fill", "t": "fg/primary"},
                {"n": "1:3", "p": "fill", "t": "fg/primary"},
            ]
        ),
        encoding="utf-8",
    )
    batch_dir = tmp_path / "apply_batches"

    first = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--emit-only",
            "--batch-size",
            "1",
            "--batch-dir",
            str(batch_dir),
        ],
        catch_exceptions=False,
    )
    assert first.exit_code == 0
    assert (batch_dir / "batch-0002.json").exists()
    unrelated = batch_dir / "notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    rows.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary"}]),
        encoding="utf-8",
    )
    second = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--emit-only",
            "--batch-size",
            "1",
            "--batch-dir",
            str(batch_dir),
        ],
        catch_exceptions=False,
    )

    assert second.exit_code == 0
    assert (batch_dir / "batch-0001.json").exists()
    assert not (batch_dir / "batch-0002.json").exists()
    assert not (batch_dir / "batch-0002.use_figma.js").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["batch_count"] == 1


def test_apply_tokens_rejects_unsupported_manifest_schema_version(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    manifest_path = tmp_path / "fixes.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 2, "fixes": []}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(manifest_path),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
        ],
    )

    assert result.exit_code != 0
    assert "unsupported apply-tokens schema_version 2" in result.output


def test_apply_tokens_rejects_manifest_target_mismatch(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    manifest_path = tmp_path / "fixes.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "file_key": "other-file",
                "page_node_id": PAGE_ID,
                "fixes": [],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(manifest_path),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
        ],
    )

    assert result.exit_code != 0
    assert "does not match --file" in result.output


def test_apply_tokens_refuses_invalid_compact_paint_index(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary", "paint_index": -1}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["counts"]["refusals"] == {"invalid_paint_index": 1}


def test_apply_tokens_refuses_stale_catalog_by_default(tmp_path: Path) -> None:
    _track_current(tmp_path, version="v3")
    _write_catalog(tmp_path, source_version="v2")
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary"}]), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
        ],
    )

    assert result.exit_code != 0
    assert "ds_catalog.json is stale" in result.output


def test_apply_tokens_refuses_stale_explicit_catalog_by_default(tmp_path: Path) -> None:
    _track_current(tmp_path, version="v3")
    _write_catalog(tmp_path, source_version="v2")
    rows = tmp_path / "bindings_for_figma.json"
    catalog = tmp_path / ".figma-sync" / "ds_catalog.json"
    rows.write_text(json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary"}]), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--catalog",
            str(catalog),
        ],
    )

    assert result.exit_code != 0
    assert "ds_catalog.json is stale" in result.output


def test_apply_tokens_refuses_non_authoritative_and_missing_key_rows(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path, source="seeded:manual", key=None)
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary"}]), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["counts"]["refusals"] == {"non_authoritative_variable": 1}

    result_allowed = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--allow-non-authoritative",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert json.loads(result_allowed.output)["counts"]["refusals"] == {"missing_variable_key": 1}


def test_apply_tokens_legacy_bindings_for_figma_allows_id_fallback_with_library(
    tmp_path: Path,
) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path, key=None)
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(json.dumps([{"n": "1:2", "p": "fill", "t": "fg/primary"}]), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--legacy-bindings-for-figma",
            "--library",
            "TapIn",
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["fixes"] == 1
    assert data["refusals"] == 0


def test_apply_tokens_writes_all_refusals_to_remaining_out(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path, source="seeded:manual")
    rows = tmp_path / "bindings_for_figma.json"
    rows.write_text(
        json.dumps(
            [
                {"n": "1:2", "p": "fill", "t": "fg/primary"},
                {"n": "1:3", "p": "fill", "t": "missing/token"},
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--remaining-out",
            "refusals.json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    payload = json.loads((tmp_path / "refusals.json").read_text(encoding="utf-8"))
    assert payload["kind"] == "figmaclaw.apply_tokens.refusals"
    assert [row["reason"] for row in payload["refusals"]] == [
        "non_authoritative_variable",
        "token_not_in_catalog",
    ]


def test_apply_tokens_rejects_aggregated_suggest_tokens_sidecar(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    sidecar = tmp_path / "page.suggestions.json"
    sidecar.write_text(
        json.dumps({"file_key": FILE_KEY, "frames": {"10:1": {"issues": []}}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(sidecar),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
        ],
    )

    assert result.exit_code != 0
    assert "suggest-tokens sidecars are aggregated by value" in result.output


def test_apply_tokens_refuses_versioned_fix_from_different_catalog_version(
    tmp_path: Path,
) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path, source_version="v2")
    manifest_path = tmp_path / "fixes.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixes": [
                    {
                        "node_id": "1:2",
                        "property": "fill",
                        "variable_id": VAR_ID,
                        "source": "figma_api",
                        "catalog_source_version": "v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(manifest_path),
            "--file",
            FILE_KEY,
            "--page",
            PAGE_ID,
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["counts"]["refusals"] == {"catalog_source_version_mismatch": 1}


def test_apply_tokens_execute_uses_shared_mcp_executor(tmp_path: Path) -> None:
    _track_current(tmp_path)
    _write_catalog(tmp_path)
    manifest_path = tmp_path / "fixes.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixes": [
                    {
                        "node_id": "1:2",
                        "property": "fill",
                        "variable_id": VAR_ID,
                        "source": "figma_api",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    executor = AsyncMock(
        return_value={
            "mode": "execute",
            "total": 1,
            "resume_from": 0,
            "executed": 1,
            "failures": 0,
            "calls": [],
        }
    )

    with patch("figmaclaw.commands.apply_tokens.execute_use_figma_calls", executor):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "apply-tokens",
                str(manifest_path),
                "--file",
                FILE_KEY,
                "--page",
                PAGE_ID,
                "--execute",
                "--batch-dir",
                "apply_batches",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    executor.assert_awaited_once()
    args, kwargs = executor.call_args
    assert args[0][0]["file_key"] == FILE_KEY
    assert kwargs["resume_from"] == 0
    data = json.loads(result.output)
    assert data["execution"]["failures"] == 0
