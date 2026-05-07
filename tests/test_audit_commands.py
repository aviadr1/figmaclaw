"""Tests for read-only audit migration commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from figmaclaw.audit import build_audit_diagnose_report
from figmaclaw.main import cli


def _audit_page_node() -> dict:
    return {
        "id": "200:1",
        "name": "Audit",
        "type": "CANVAS",
        "children": [
            {
                "id": "300:1",
                "name": "clone bound",
                "type": "RECTANGLE",
                "fills": [
                    {
                        "type": "SOLID",
                        "color": {"r": 1, "g": 0, "b": 0, "a": 1},
                        "boundVariables": {"color": {"id": "VariableID:lib/1:1"}},
                    }
                ],
            },
            {
                "id": "300:2",
                "name": "clone literal",
                "type": "RECTANGLE",
                "fills": [{"type": "SOLID", "color": {"r": 0.913, "g": 0, "b": 0.384, "a": 1}}],
            },
        ],
    }


def _fake_client(page: dict) -> MagicMock:
    client = MagicMock()
    client.get_nodes = AsyncMock(return_value={"200:1": page})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def test_audit_page_check_reports_business_status_without_failing_exit(
    tmp_path: Path, monkeypatch
) -> None:
    """Canon: migration misses are JSON status, not command failure."""
    manifest = tmp_path / "bindings_for_figma.json"
    manifest.write_text(
        json.dumps(
            [
                {"n": "10:1", "p": "fill", "t": "fg/brand", "v": "#E90062"},
                {"n": "10:2", "p": "fill", "t": "fg/brand", "v": "#E90062"},
                {"n": "10:3", "p": "fill", "t": "fg/brand", "v": "#E90062"},
            ]
        ),
        encoding="utf-8",
    )
    idmap = tmp_path / "idmap.json"
    idmap.write_text(json.dumps({"10:1": "300:1", "10:2": "300:2"}), encoding="utf-8")
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")

    with patch(
        "figmaclaw.commands.audit_page.FigmaClient", return_value=_fake_client(_audit_page_node())
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "audit-page",
                "check",
                "file123",
                "200:1",
                "--manifest",
                str(manifest),
                "--idmap",
                str(idmap),
                "--json",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["counts"] == {"bound": 1, "missing_or_literal": 1, "missing_idmap": 1}
    assert {row["status"] for row in data["misses"]} == {"missing_or_literal", "missing_idmap"}
    assert "does not prove exact token identity" in data["limitation"]


def test_audit_page_check_writes_reports_only_when_requested(tmp_path: Path, monkeypatch) -> None:
    """Canon: audit reports are explicit operator output, not default durable state."""
    manifest = tmp_path / "bindings_for_figma.json"
    manifest.write_text(json.dumps([{"n": "10:2", "p": "fill", "t": "fg/brand"}]), encoding="utf-8")
    idmap = tmp_path / "idmap.json"
    idmap.write_text(json.dumps({"10:2": "300:2"}), encoding="utf-8")
    out = tmp_path / "report.json"
    remaining = tmp_path / "remaining.json"
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")

    with patch(
        "figmaclaw.commands.audit_page.FigmaClient", return_value=_fake_client(_audit_page_node())
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "audit-page",
                "check",
                "file123",
                "200:1",
                "--manifest",
                str(manifest),
                "--idmap",
                str(idmap),
                "--out",
                str(out),
                "--remaining-out",
                str(remaining),
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert json.loads(out.read_text())["counts"]["missing_or_literal"] == 1
    assert json.loads(remaining.read_text())["rows"] == [
        {"n": "10:2", "p": "fill", "t": "fg/brand"}
    ]


def test_audit_page_diagnose_uses_explicit_palettes(tmp_path: Path, monkeypatch) -> None:
    """Canon D12: palette identity is explicit input, not hardcoded command knowledge."""
    old_palette = tmp_path / "old.json"
    old_palette.write_text(json.dumps({"#E90062": "old brand"}), encoding="utf-8")
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")

    with patch(
        "figmaclaw.commands.audit_page.FigmaClient", return_value=_fake_client(_audit_page_node())
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "audit-page",
                "diagnose",
                "file123",
                "200:1",
                "--old-palette",
                str(old_palette),
                "--json",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["bound_paints"] == 1
    assert data["unbound_paints"] == 1
    assert data["counts"]["old_palette_literal"] == 1
    assert data["old_palette"] == {"#E90062": "old brand"}


def test_audit_page_diagnose_counts_partial_node_level_paint_bindings() -> None:
    """INVARIANT: one bound fill slot does not make sibling literal fills bound."""
    page = {
        "id": "200:1",
        "name": "Audit",
        "type": "CANVAS",
        "children": [
            {
                "id": "300:1",
                "name": "mixed fills",
                "type": "RECTANGLE",
                "fills": [
                    {"type": "SOLID", "color": {"r": 1, "g": 0, "b": 0, "a": 1}},
                    {"type": "SOLID", "color": {"r": 0.913, "g": 0, "b": 0.384, "a": 1}},
                ],
                "boundVariables": {"fills": [{"id": "VariableID:lib/1:1"}, None]},
            }
        ],
    }

    report = build_audit_diagnose_report(
        page,
        audit_page_id="200:1",
        old_palette={"#E90062": "old brand"},
    )

    assert report.bound_paints == 1
    assert report.unbound_paints == 1
    assert report.counts["old_palette_literal"] == 1
    assert report.findings[0].node_id == "300:1"


def _valid_component_map(new_key: str = "new-key", name: str = "button") -> dict:
    return {
        "schema_version": 3,
        "rules": [
            {
                "old_component_set": "Button",
                "old_key": "old-key",
                "target": {
                    "status": "replace_with_new_component",
                    "new_key": new_key,
                    "expected_type": "COMPONENT_SET",
                    "expected_new_name": name,
                },
                "swap_strategy": "create-instance-and-translate",
                "parent_handling": "leave-as-instance",
                "property_translation": {"kind": "copy-compatible"},
                "validation": {
                    "assert_target_type": True,
                    "assert_name_matches": True,
                    "assert_property_keys": True,
                    "assert_variant_axes": False,
                },
            }
        ],
    }


def test_audit_pipeline_lint_warns_when_target_registry_not_probed(tmp_path: Path) -> None:
    """Canon REG-1: missing registry is unknown, not probed-empty."""
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(json.dumps(_valid_component_map()), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "audit-pipeline",
            "lint",
            "--component-map",
            str(component_map),
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["target_registry_state"] == "not_probed"
    assert data["counts"]["warning"] == 1


def test_audit_pipeline_lint_checks_targets_against_census(tmp_path: Path) -> None:
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(json.dumps(_valid_component_map()), encoding="utf-8")
    census = tmp_path / "_census.md"
    census.write_text(
        "\n".join(
            [
                "---",
                "component_set_count: 1",
                "---",
                "| Component set | Key | Page | Updated |",
                "|---|---|---|---|",
                "| `button` | `new-key` | Components | 2026-01-01 |",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "audit-pipeline",
            "lint",
            "--component-map",
            str(component_map),
            "--census",
            str(census),
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["target_registry_state"] == "probed_with_entries"
    assert data["findings"] == []


def test_audit_pipeline_lint_reports_census_name_mismatch(tmp_path: Path) -> None:
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(json.dumps(_valid_component_map(name="button")), encoding="utf-8")
    census = tmp_path / "_census.md"
    census.write_text(
        "| Component set | Key | Page | Updated |\n"
        "|---|---|---|---|\n"
        "| `text-input` | `new-key` | Components | 2026-01-01 |\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "audit-pipeline",
            "lint",
            "--component-map",
            str(component_map),
            "--census",
            str(census),
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["counts"]["error"] == 1
    assert "does not match census name" in data["findings"][0]["message"]


def test_audit_pipeline_lint_counts_non_object_rule_once(tmp_path: Path) -> None:
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(json.dumps({"schema_version": 3, "rules": ["bad"]}), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "audit-pipeline",
            "lint",
            "--component-map",
            str(component_map),
            "--json",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["counts"]["error"] == 1
