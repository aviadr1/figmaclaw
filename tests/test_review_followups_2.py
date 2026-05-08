"""Tests for the second-round PR #167 review fixes.

Each test names the finding it protects against. See the second review pass
report for the full list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from figmaclaw.audit import (
    EXPECTED_TYPES,
    validate_rule_against_census,
    validate_rule_variant_mapping,
)
from figmaclaw.audit_page_primitives import _INACTIVE_NAME_KEYWORD
from figmaclaw.audit_page_swap import SwapManifest
from figmaclaw.component_map import (
    FLAT_RULE_DISCRIMINATOR_ERROR_PREFIX,
    FLAT_SWAP_STRATEGIES,
    NestedRule,
    VariantTaxonomyDocument,
    parse_flat_rule,
)
from figmaclaw.main import cli

# Finding #1 — `<library>:` prefix in compact-row token name --------------


def _make_repo(tmp_path: Path) -> None:
    from figmaclaw.figma_sync_state import FigmaSyncState

    state = FigmaSyncState(tmp_path)
    state.add_tracked_file("file123", "DS")
    state.set_file_meta(
        "file123",
        version="v2",
        last_modified="2026-05-08T00:00:00Z",
        last_checked_at="2026-05-08T00:00:00Z",
        file_name="DS",
    )
    state.save()
    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "libraries": {
                    "libabc": {
                        "name": "TAP IN",
                        "source_file_key": "file123",
                        "source_version": "v2",
                        "source": "figma_api",
                    }
                },
                "variables": {
                    "VariableID:libabc/1:1": {
                        "library_hash": "libabc",
                        "collection_id": "c1",
                        "name": "fg/inverse",
                        "key": "fg-inverse-key",
                        "resolved_type": "COLOR",
                        "values_by_mode": {"light": {"hex": "#FFF"}},
                        "source": "figma_api",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_finding_1_tapin_prefix_resolves_to_bare_token(tmp_path: Path) -> None:
    """A `<library>:<token_name>` row resolves to the bare token name."""
    _make_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "tapin:fg/inverse"}]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows_path),
            "--file",
            "file123",
            "--page",
            "10:1",
            "--legacy-bindings-for-figma",
            "--library",
            "TAP IN",
            "--json",
        ],
        catch_exceptions=False,
    )
    data = json.loads(result.output)
    assert data["fixes"] == 1, result.output
    assert data["refusals"] == 0


def test_finding_1_unknown_prefix_token_refusal_includes_did_you_mean(
    tmp_path: Path,
) -> None:
    """If even the stripped form misses, the refusal still names the candidate."""
    _make_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "tapin:nonsuch"}]),
        encoding="utf-8",
    )
    CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows_path),
            "--file",
            "file123",
            "--page",
            "10:1",
            "--remaining-out",
            "remaining.json",
            "--legacy-bindings-for-figma",
            "--library",
            "TAP IN",
            "--json",
        ],
        catch_exceptions=False,
    )
    remaining = json.loads((tmp_path / "remaining.json").read_text(encoding="utf-8"))
    payload = remaining["refusals"][0]["row"]
    assert payload["did_you_mean_token_name"] == "nonsuch"
    assert "library" in (payload.get("hint") or "").lower()


# Finding #3 — census error path differs by rule shape -------------------


def test_finding_3_flat_rule_census_error_uses_top_level_path() -> None:
    """A v3-flat rule whose new_key isn't in the census reports `rules[i].new_key`."""
    rule = parse_flat_rule(
        {
            "old_key": "ok",
            "swap_strategy": "direct",
            "new_key": "wrong-key",
            "new_component_set": "alert",
            "variant_mapping": {"X": "y"},
        }
    )
    findings = validate_rule_against_census(
        rule, idx=4, census={"right-key": "alert"}, target_registry_state="probed_with_entries"
    )
    assert findings
    msg = findings[0].message or ""
    assert "rules[4].new_key" in msg
    assert "rules[4].target.new_key" not in msg


def test_finding_3_nested_rule_census_error_keeps_target_path() -> None:
    rule = NestedRule.model_validate(
        {
            "old_component_set": "Btn",
            "old_key": "ok",
            "target": {
                "status": "replace_with_new_component",
                "new_key": "wrong-key",
                "expected_type": "COMPONENT_SET",
                "expected_new_name": "alert",
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
    )
    findings = validate_rule_against_census(
        rule, idx=2, census={}, target_registry_state="probed_with_entries"
    )
    assert findings
    assert "rules[2].target.new_key" in (findings[0].message or "")


# Finding #4 — empty taxonomy axes does not misclassify -------------------


def test_finding_4_empty_axes_warns_instead_of_misclassifying() -> None:
    """A taxonomy entry with empty axes emits one warning, no chain errors."""
    rule = parse_flat_rule(
        {
            "old_key": "ok",
            "swap_strategy": "direct",
            "new_key": "incomplete",
            "new_component_set": "X",
            "variant_mapping": {"Type": "Logo"},
        }
    )
    doc = VariantTaxonomyDocument.model_validate({"incomplete": {"axes": {}}})
    findings = validate_rule_variant_mapping(rule, 0, doc.component_sets)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.status == "warning"
    msg = finding.message or ""
    assert "no published axes" in msg
    assert "could not be parsed" not in msg, "should not chain-error on classification"


# Finding #5 — `obsolete` keyword appears once in the regex ----------------


def test_finding_5_obsolete_listed_once() -> None:
    pattern = _INACTIVE_NAME_KEYWORD.pattern
    assert pattern.count("obsolete") == 1


# Finding #7 — SwapManifest schema_version is constrained -----------------


def test_finding_7_swap_manifest_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValidationError):
        SwapManifest.model_validate({"schema_version": 2, "rows": []})


# Finding #8 — EXPECTED_TYPES includes None -------------------------------


def test_finding_8_expected_types_includes_none() -> None:
    """The legacy contract treats `None` as a valid omission."""
    assert None in EXPECTED_TYPES


# Finding #9 — human output prints finding messages -----------------------


def test_finding_9_lint_human_output_lists_finding_messages(tmp_path: Path) -> None:
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rules": [
                    {
                        "old_component_set": "Toast",
                        "old_key": "tk",
                        "swap_strategy": "recompose_local",
                        # missing recomposition_plan → error
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
            "audit-pipeline",
            "lint",
            "--component-map",
            str(component_map),
        ],
        catch_exceptions=False,
    )
    # Human output contains the actual error message text, not just counts.
    assert "recomposition_plan" in result.output
    assert "[error]" in result.output


# Finding #11 — swap refuses when every row lacks oldCid -----------------


def test_finding_11_swap_refuses_emit_when_every_row_lacks_oldcid(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "swap.json"
    manifest.write_text(
        json.dumps([{"src": "1", "newKey": "K"}, {"src": "2", "newKey": "K"}]),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "audit-page",
            "swap",
            "FILE",
            "9559:29",
            "--manifest",
            str(manifest),
            "--emit-only",
            "--batch-dir",
            "swap_batches",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "every row in the manifest lacks oldCid" in result.output


# Finding #12 — sentinel-prefixed discriminator ValueError ----------------


def test_finding_12_discriminator_error_carries_sentinel_prefix() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_flat_rule({"old_key": "ok", "new_key": "nk", "swap_strategy": "garbage"})
    assert str(exc_info.value).startswith(FLAT_RULE_DISCRIMINATOR_ERROR_PREFIX)
    # The message lists every published swap_strategy so a future addition
    # surfaces in the lint output rather than silently passing the test.
    msg = str(exc_info.value)
    for value in FLAT_SWAP_STRATEGIES:
        assert value in msg


# Cross-prefix cleanup test (finding #14) --------------------------------


def test_finding_14_clean_generated_batch_dir_preserves_other_prefix(
    tmp_path: Path,
) -> None:
    """A `swap-batch-NNNN.json` survives a cleanup keyed to `batch` prefix."""
    from figmaclaw.use_figma_batches import clean_generated_batch_dir

    (tmp_path / "swap-batch-0001.json").write_text("[]", encoding="utf-8")
    (tmp_path / "batch-0001.json").write_text("[]", encoding="utf-8")
    clean_generated_batch_dir(tmp_path, file_name_prefix="batch")
    assert (tmp_path / "swap-batch-0001.json").exists(), (
        "cleanup keyed to a different prefix must not delete files matching another"
    )
    assert not (tmp_path / "batch-0001.json").exists()


# README + docs touched (finding #2) -------------------------------------


def test_finding_2_readme_documents_audit_page_swap() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "audit-page swap" in readme
    assert "audit-pipeline lint" in readme
    assert "--variants" in readme


def test_finding_2_docs_describe_v3_flat_schema() -> None:
    doc = (Path(__file__).resolve().parents[1] / "docs" / "migration-pipeline.md").read_text(
        encoding="utf-8"
    )
    assert "v3-flat" in doc.lower() or "Flat v3" in doc
    assert "swap_strategy" in doc
    assert "recompose_local" in doc
    assert "audit_only" in doc
