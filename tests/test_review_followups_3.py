"""Tests for the third-round PR #167 review fixes.

Each test names the finding it protects against. See the third review pass
report for the full list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from figmaclaw.audit import (
    build_pipeline_lint_report,
    validate_rule_variant_mapping,
)
from figmaclaw.audit_page_swap import (
    SwapRow,
    load_swap_manifest,
    render_swap_script,
)
from figmaclaw.component_map import VariantTaxonomyDocument, parse_flat_rule
from figmaclaw.main import cli

# Finding #1 — pickVariantChild requires exact axis-set match -------------


def test_review3_finding_1_pick_variant_requires_exact_axis_match() -> None:
    """The rendered JS rejects subset matches, eliminating order-dependent picks.

    A child named `Type=Logo, Colored=True` must NOT match a request that
    only specifies `Type=Logo` — the operator's intent is ambiguous in
    that case (Colored=True or False?), and previously the function picked
    the first child that satisfied the subset, leaking `children` ordering
    into the swap result.
    """
    js = render_swap_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[SwapRow(src="a", newKey="b")],
    )
    # The new logic compares haveKeys.length to wantedKeys.length explicitly.
    assert "haveKeys.length !== wantedKeys.length" in js
    # And guards against a subset match by `continue`-ing on length mismatch.
    assert "if (haveKeys.length !== wantedKeys.length) continue;" in js


# Finding #2 — _reject_duplicate_src caps the message ---------------------


def test_review3_finding_2_duplicate_src_message_capped(tmp_path: Path) -> None:
    """A 1k-row dup-heavy manifest produces a bounded, readable error."""
    payload = []
    for i in range(50):  # 50 distinct duplicate-pairs
        payload.append({"src": f"src{i}", "newKey": "K"})
        payload.append({"src": f"src{i}", "newKey": "K"})
    with pytest.raises(ValueError) as exc_info:
        load_swap_manifest(payload)
    msg = str(exc_info.value)
    # Cap at 5 examples in the message; the rest go in `… and N more`.
    assert "and 45 more" in msg
    # Sanity: we don't try to list every src.
    assert msg.count("@rows") <= 5


# Finding #3 — did_you_mean carried into downstream refusals --------------


def _make_catalog_with_unbindable(tmp_path: Path) -> None:
    """Build a repo whose catalog variable lacks `key` so the downstream gate fires."""
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
    # Variable has no `key` AND its source isn't authoritative — so
    # _catalog_refusal will fire AFTER the prefix-strip retry succeeds.
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "libraries": {
                    "libabc": {
                        "name": "TAP IN",
                        "source_file_key": "file123",
                        "source_version": "v2",
                        "source": "manual",
                    }
                },
                "variables": {
                    "VariableID:libabc/1:1": {
                        "library_hash": "libabc",
                        "collection_id": "c1",
                        "name": "fg/inverse",
                        "resolved_type": "COLOR",
                        "values_by_mode": {"light": {"hex": "#FFF"}},
                        "source": "manual",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_review3_finding_3_prefix_hint_present_in_downstream_refusal(
    tmp_path: Path,
) -> None:
    """Even when the variable resolves but a downstream gate refuses, the
    `did_you_mean_token_name` hint surfaces so the operator sees WHY the
    catalog identity differs from the input row."""
    _make_catalog_with_unbindable(tmp_path)
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
            "--remaining-out",
            "remaining.json",
            "--legacy-bindings-for-figma",
            "--library",
            "TAP IN",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    remaining = json.loads((tmp_path / "remaining.json").read_text(encoding="utf-8"))
    assert remaining["refusals"], "downstream gate should have refused this row"
    payload = remaining["refusals"][0]["row"]
    # The catalog gate produced the refusal AND the prefix hint is preserved.
    assert payload["did_you_mean_token_name"] == "fg/inverse"


# Finding #4 — VariantAxis.inner_instance honoured ------------------------


def test_review3_finding_4_inner_instance_axis_skips_value_membership_check() -> None:
    """An axis flagged inner_instance accepts arbitrary string values.

    Models the F23 slot-on-the-leaf case (text-input's `_input-add-on
    type=flag`). Without this branch the lint would reject the operator's
    swap-manifest value as not-in-the-published-set even though the value
    is a component_set key, not a published variant string.
    """
    rule = parse_flat_rule(
        {
            "old_key": "ok",
            "swap_strategy": "direct",
            "new_key": "text_input",
            "variant_mapping": {"_input-add-on": "flag-component-set-key"},
        }
    )
    doc = VariantTaxonomyDocument.model_validate(
        {
            "text_input": {
                "axes": {
                    "_input-add-on": {
                        "values": ["none"],
                        "inner_instance": True,
                    }
                }
            }
        }
    )
    findings = validate_rule_variant_mapping(rule, 0, doc.component_sets)
    assert findings == [], f"inner_instance axis should skip value-membership check; got {findings}"


# Finding #7 — empty-string variant value gives a clear error -------------


def test_review3_finding_7_empty_variant_value_gives_explicit_error() -> None:
    rule = parse_flat_rule(
        {
            "old_key": "ok",
            "swap_strategy": "direct",
            "new_key": "btn",
            "variant_mapping": {"Type": ""},
        }
    )
    doc = VariantTaxonomyDocument.model_validate(
        {"btn": {"axes": {"Type": {"values": ["A", "B"]}}}}
    )
    findings = validate_rule_variant_mapping(rule, 0, doc.component_sets)
    assert findings, "empty value should not silently pass"
    msg = findings[0].message or ""
    assert "empty string" in msg.lower(), f"got: {msg}"
    # The misleading "could not be parsed as `axis=value`" message must NOT fire.
    assert "could not be parsed" not in msg


# Finding #6 — high-percentage missing-oldCid warning --------------------


def test_review3_finding_6_high_unknown_oldcid_ratio_emits_elevated_warning(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "swap.json"
    payload = [
        {"src": "1", "oldCid": "OLD", "newKey": "K", "variants": {"X": "y"}},
        # 4 rows missing oldCid → 80% missing → above 25% threshold.
        {"src": "2", "newKey": "K", "variants": {"X": "y"}},
        {"src": "3", "newKey": "K", "variants": {"X": "y"}},
        {"src": "4", "newKey": "K", "variants": {"X": "y"}},
        {"src": "5", "newKey": "K", "variants": {"X": "y"}},
    ]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
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
    warnings_text = " ".join(data["warnings"])
    assert "almost always indicates the wrong manifest" in warnings_text


# Finding #8 — lint output shows rule's old_component_set ----------------


def test_review3_finding_8_lint_human_output_shows_rule_label(
    tmp_path: Path,
) -> None:
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rules": [
                    {
                        "old_component_set": "Buttons Desktop",
                        "old_key": "btn-old",
                        "swap_strategy": "recompose_local",
                        # missing recomposition_plan → emits an error
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
    # Human output prepends the rule's old_component_set label so operators
    # can identify which rule failed without re-running with --json.
    assert "(Buttons Desktop)" in result.output


def test_review3_finding_8_lint_report_carries_rule_label_in_json(
    tmp_path: Path,
) -> None:
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rules": [
                    {
                        "old_component_set": "Toast",
                        "old_key": "toast-old",
                        "swap_strategy": "recompose_local",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = build_pipeline_lint_report(component_map)
    error_findings = [f for f in report.findings if f.status == "error"]
    assert error_findings
    assert all(f.rule_label == "Toast" for f in error_findings)
