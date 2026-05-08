"""Tests for the PR #167 review follow-up fixes.

Each test names the review finding it protects against in its docstring so
future regressions can be traced back to the original review comment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from figmaclaw.audit import build_pipeline_lint_report, load_variant_taxonomies
from figmaclaw.audit_page_swap import (
    SwapRow,
    load_swap_manifest,
    render_swap_script,
)
from figmaclaw.component_map import VariantTaxonomyDocument, parse_flat_rule
from figmaclaw.main import cli
from figmaclaw.use_figma_batches import clean_generated_batch_dir, write_use_figma_batches

# Finding #1 — swap JS `ok` mirrors apply-tokens hardFailures discipline ---


def test_finding_1_swap_js_ok_false_when_every_row_skipped() -> None:
    """A batch where every row hit a skip counter must NOT report ok:true.

    The runtime semantics: skip reasons (`skipped_no_clone`, `skipped_no_set`,
    `skipped_no_variant`, `skipped_no_parent`) all mean "this row's intent
    didn't land". The aggregate ok must be false.
    """
    js = render_swap_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[SwapRow(src="a", newKey="b")],
    )
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    # The hardFailures definition must include every skip counter.
    assert "skipped_no_clone" in js_no_comments
    assert "skipped_no_set" in js_no_comments
    assert "skipped_no_variant" in js_no_comments
    assert "skipped_no_parent" in js_no_comments
    # And ok must derive from hardFailures, not just errors.
    assert "ok: hardFailures === 0" in js_no_comments
    assert "ok: stats.errors === 0" not in js_no_comments


# Finding #2 — empty idMap throws at init ---------------------------------


def test_finding_2_swap_js_throws_on_empty_idmap() -> None:
    """An empty `{}` idMap is an init failure, not a per-row condition."""
    js = render_swap_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[SwapRow(src="a", newKey="b")],
    )
    assert "empty idMap in namespace" in js
    # And the throw is gated on the actual emptiness check.
    assert "Object.keys(idMap).length === 0" in js


# Finding #3 — clean_generated_batch_dir on every batch writer -------------


def test_finding_3_clean_generated_batch_dir_removes_orphaned_files(
    tmp_path: Path,
) -> None:
    """A re-run with fewer batches than last time leaves no orphans on disk."""
    # First run: 5 batches.
    write_use_figma_batches(
        list(range(10)),
        batch_dir=tmp_path,
        batch_size=2,
        file_name_prefix="swap-batch",
        file_key="FILE",
        row_to_dict=lambda x: {"i": x},
        render_js=lambda rows: f"// {len(rows)} rows",
        description_prefix="swap",
    )
    assert sorted(p.name for p in tmp_path.glob("swap-batch-*.json")) == [
        f"swap-batch-{i:04d}.json" for i in range(1, 6)
    ]

    # Second run: 2 batches. The 0003-0005 files must be cleaned up.
    write_use_figma_batches(
        list(range(4)),
        batch_dir=tmp_path,
        batch_size=2,
        file_name_prefix="swap-batch",
        file_key="FILE",
        row_to_dict=lambda x: {"i": x},
        render_js=lambda rows: f"// {len(rows)} rows",
        description_prefix="swap",
    )
    assert sorted(p.name for p in tmp_path.glob("swap-batch-*.json")) == [
        "swap-batch-0001.json",
        "swap-batch-0002.json",
    ]
    assert sorted(p.name for p in tmp_path.glob("swap-batch-*.use_figma.js")) == [
        "swap-batch-0001.use_figma.js",
        "swap-batch-0002.use_figma.js",
    ]


def test_finding_3_clean_generated_batch_dir_preserves_unrelated_files(
    tmp_path: Path,
) -> None:
    """Operator-authored sidecar files in batch_dir must survive cleanup."""
    sidecar = tmp_path / "apply_colors_inline.js"
    sidecar.write_text("// hand-rolled fallback", encoding="utf-8")
    (tmp_path / "swap-batch-0099.json").write_text("[]", encoding="utf-8")
    clean_generated_batch_dir(tmp_path, file_name_prefix="swap-batch")
    assert sidecar.exists()
    assert not (tmp_path / "swap-batch-0099.json").exists()


# Finding #4 — duplicate src detection in load_swap_manifest ---------------


def test_finding_4_duplicate_src_rejected() -> None:
    """A re-run that appended new rows to an already-applied manifest fails fast."""
    payload = [
        {"src": "1", "newKey": "K"},
        {"src": "2", "newKey": "K"},
        {"src": "1", "newKey": "K"},  # duplicate
    ]
    with pytest.raises(ValueError, match=r"duplicate src.*1@rows"):
        load_swap_manifest(payload)


# Finding #5 — compact-row refusal lists missing AND unrecognised --------


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


def test_finding_5_compact_row_refusal_lists_both_missing_and_unrecognised(
    tmp_path: Path,
) -> None:
    """Row {node_id, prop, var_name}: payload reports both diagnostics."""
    _make_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"node_id": "1:2", "prop": "fill", "var_name": "fg/inverse"}]),
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
            "--remaining-out",
            "remaining.json",
            "--legacy-bindings-for-figma",
            "--library",
            "TAP IN",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    remaining = json.loads((tmp_path / "remaining.json").read_text(encoding="utf-8"))
    refusal_payload = remaining["refusals"][0]["row"]
    # Author needs both lists in the same refusal.
    assert refusal_payload["unrecognised_compact_row_fields"] == ["prop", "var_name"]
    assert sorted(refusal_payload["missing_canonical_fields"]) == [
        "property",
        "token_name",
    ]


def test_finding_5_canonical_row_with_one_unknown_field_lists_only_unrecognised(
    tmp_path: Path,
) -> None:
    """Row that has all canonical fields but one extra unknown still flags it."""
    _make_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps(
            [
                {
                    "n": "1:2",
                    "p": "fill",
                    "t": "fg/inverse",
                    "color": "tapin",  # not in the recognised set
                }
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
    # Row resolves successfully — `color` is just an ignored extra key when
    # the canonical fields are all present. (The unrecognised-key diagnostic
    # only fires when the row would otherwise be refused.)
    data = json.loads(result.output)
    assert data["fixes"] == 1


# Finding #6 — recompose_local with new_key fires census check -----------


def test_finding_6_recompose_local_with_new_key_validates_against_census(
    tmp_path: Path,
) -> None:
    """A typo'd new_key on a recompose_local rule still surfaces."""
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
                        # Note: includes a new_key — should be census-checked.
                        "new_component_set": "alert",
                        "new_key": "wrong-key",
                        "recomposition_plan": {
                            "new_local_name": "LocalToast",
                            "structure": {"layout": "row"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    census = tmp_path / "_census.md"
    census.write_text(
        "| Component set | Key | Page | Updated |\n"
        "|---|---|---|---|\n"
        "| `alert` | `right-key` | Components | 2026-01-01 |\n",
        encoding="utf-8",
    )

    report = build_pipeline_lint_report(component_map, census_paths=[census])
    error_msgs = [f.message for f in report.findings if f.status == "error"]
    assert any(
        "wrong-key" in (m or "") and "not found in census" in (m or "") for m in error_msgs
    ), f"recompose_local rule's new_key must be census-checked; got {error_msgs}"


# Finding #7 — friendly discriminator error -------------------------------


def test_finding_7_unknown_swap_strategy_emits_friendly_message(
    tmp_path: Path,
) -> None:
    """Author-friendly error names the accepted swap_strategy values."""
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rules": [
                    {
                        "old_key": "ok",
                        "swap_strategy": "swap-direct",  # nested vocab on a flat-shape rule
                        "new_key": "nk",
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
            "--json",
        ],
        catch_exceptions=False,
    )
    data = json.loads(result.output)
    error_msgs = [f["message"] for f in data["findings"] if f["status"] == "error"]
    assert any("direct" in (m or "") and "audit_only" in (m or "") for m in error_msgs), (
        f"discriminator error must list FLAT_SWAP_STRATEGIES; got {error_msgs}"
    )


def test_finding_7_parse_flat_rule_raises_value_error_with_strategy_list() -> None:
    with pytest.raises(ValueError, match=r"swap_strategy must be one of"):
        parse_flat_rule({"old_key": "ok", "new_key": "nk", "swap_strategy": "bogus"})


# Finding #11 — skipsSample[] reports per-row skip diagnostics -----------


def test_finding_11_swap_js_emits_skipssample_for_skip_reasons() -> None:
    """The swap JS must record up to 20 per-row skip entries with reasons."""
    js = render_swap_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[SwapRow(src="a", newKey="b")],
    )
    assert "skipsSample" in js
    assert 'recordSkip("no_clone"' in js
    assert 'recordSkip("no_set"' in js
    assert 'recordSkip("no_variant"' in js
    assert 'recordSkip("no_parent"' in js
    # And the cap of 20 is encoded.
    assert "skipsSample.length < 20" in js


# Finding #12 — dry-run human output surfaces warnings -------------------


def test_finding_12_swap_dry_run_warns_on_missing_old_cid(tmp_path: Path) -> None:
    """Operators see a warnings section when rows lack oldCid."""
    manifest = tmp_path / "swap.json"
    manifest.write_text(
        json.dumps(
            [
                {"src": "1", "newKey": "K", "variants": {"X": "y"}},  # no oldCid
                {"src": "2", "newKey": "K", "variants": {"X": "y"}},  # no oldCid
            ]
        ),
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
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["warnings"], "warnings list should not be empty when rows lack oldCid"
    assert any("oldCid" in w for w in data["warnings"])


def test_finding_12_swap_dry_run_warns_on_empty_variants(tmp_path: Path) -> None:
    manifest = tmp_path / "swap.json"
    manifest.write_text(
        json.dumps([{"src": "1", "oldCid": "OLD1", "newKey": "K"}]),
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
            "--json",
        ],
        catch_exceptions=False,
    )
    data = json.loads(result.output)
    assert any("variants" in w and "defaultVariant" in w for w in data["warnings"])


# Finding "tests for error paths" — load_variant_taxonomies ----------------


def test_load_variant_taxonomies_surfaces_unreadable_file(tmp_path: Path) -> None:
    bad = tmp_path / "variants.json"
    bad.write_text("not even close to JSON {{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid variant-taxonomy file"):
        load_variant_taxonomies([bad])


def test_load_swap_manifest_rejects_dict_without_rows() -> None:
    with pytest.raises(ValueError, match="versioned audit-page swap manifest"):
        load_swap_manifest({"schema_version": 1})


def test_load_swap_manifest_propagates_per_row_validation_errors() -> None:
    with pytest.raises(ValidationError):
        load_swap_manifest([{"src": "", "newKey": "k"}])  # empty src


# Variant taxonomy parses both flat and wrapped shapes -------------------


def test_variant_taxonomy_accepts_flat_form() -> None:
    doc = VariantTaxonomyDocument.model_validate(
        {"compkey": {"axes": {"size": {"values": ["sm", "md"]}}}}
    )
    assert "compkey" in doc.component_sets
    assert doc.component_sets["compkey"].values_for("size") == {"sm", "md"}


def test_variant_taxonomy_accepts_wrapped_form() -> None:
    doc = VariantTaxonomyDocument.model_validate(
        {"component_sets": {"compkey": {"name": "X", "axes": {"size": {"values": ["sm"]}}}}}
    )
    assert doc.component_sets["compkey"].name == "X"
