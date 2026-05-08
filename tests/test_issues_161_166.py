"""Invariant-based tests for issues #161 - #166.

These tests focus on the user-visible invariants the bugs violated, not on
asserting that the specific bug existed:

* #161 — emitted apply-tokens JS never ``throw``s on aggregate stats; only
  init failures (target page, idMap) are allowed to throw.
* #162 — ``audit-page swap`` consumes a typed manifest, emits F17/F22/F30
  -compliant JS, and persists the SPD idMap update.
* #163 — ``audit-pipeline lint --variants`` validates axis names + values
  against the published taxonomy, including OLD-axis coverage.
* #164 — lint accepts both v3-nested and v3-flat rule shapes; the flat
  shape's discriminated union covers ``direct`` / ``recompose_local`` /
  ``audit_only``.
* #165 — compact-row refusals call out the *unrecognised* field names so the
  author knows what to rename.
* #166 — ``audit-page emit-clone-script`` warns when the source node looks
  like a non-active page (audit clone, archive, playground, draft, etc.).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from figmaclaw.apply_tokens import (
    APPLY_TOKENS_JS_TEMPLATE,
    render_apply_tokens_script,
)
from figmaclaw.audit import (
    parse_variant_axis_assignment,
    validate_rule_variant_mapping,
)
from figmaclaw.audit_page_primitives import looks_like_inactive_page_name
from figmaclaw.audit_page_swap import (
    SwapManifest,
    SwapRow,
    load_swap_manifest,
    render_swap_script,
)
from figmaclaw.component_map import (
    FlatDirectRule,
    NestedRule,
    VariantTaxonomyDocument,
    parse_flat_rule,
)
from figmaclaw.main import cli

# Issue #161 — apply-tokens JS no-throw on hardFailures ----------------------


def test_issue_161_apply_tokens_js_template_never_throws_on_hard_failures() -> None:
    """INVARIANT (F30): aggregate hardFailures must not abort the batch.

    The plugin runtime treats a thrown exception as a transaction failure
    and rolls back every per-row write that already succeeded. Per-row
    failures must report via stats counters and never via a terminal throw.
    """
    js = render_apply_tokens_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[],
        node_map="shared-plugin-data",
    )
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    # Only init-failure throws are allowed.
    init_throws = re.findall(r"throw new Error\(`[^`]+`\)", js_no_comments)
    for thrown in init_throws:
        assert "target page not found" in thrown or "missing idMap" in thrown, (
            f"emitted JS contains a non-init throw: {thrown!r}; F30 forbids "
            "throwing on aggregate stats because the runtime would roll back "
            "every successful per-row write in this batch"
        )


def test_issue_161_apply_tokens_js_template_returns_summary() -> None:
    """The JS returns an aggregate summary object so the caller can decide."""
    assert "return summary;" in APPLY_TOKENS_JS_TEMPLATE
    # The summary still encodes ok = (hardFailures === 0), so the caller can
    # implement run-level pass/fail externally.
    assert "ok: hardFailures === 0," in APPLY_TOKENS_JS_TEMPLATE


# Issue #162 — audit-page swap ----------------------------------------------


def test_issue_162_swap_manifest_accepts_resolver_output_shape() -> None:
    """INVARIANT: the bare-list resolver output is a valid swap manifest."""
    payload = [
        {
            "src": "8102:1990",
            "oldCid": "8009:29",
            "newKey": "e81fbd3e7c55508994f4630923b16d61f349eabf",
            "variants": {"Type": "Logo", "Colored": "True"},
            "props": {},
            "preserveText": True,
        }
    ]
    manifest = load_swap_manifest(payload)
    assert isinstance(manifest, SwapManifest)
    assert manifest.rows[0].src == "8102:1990"
    assert manifest.rows[0].new_key == "e81fbd3e7c55508994f4630923b16d61f349eabf"
    assert manifest.rows[0].variants == {"Type": "Logo", "Colored": "True"}
    assert manifest.rows[0].preserve_text is True


def test_issue_162_swap_manifest_rejects_non_string_variant_values() -> None:
    with pytest.raises(ValidationError):
        SwapRow.model_validate({"src": "a", "newKey": "b", "variants": {"X": 7}})


def test_issue_162_swap_js_template_satisfies_F17_F22_F30() -> None:
    """INVARIANT — the swap script template upholds the three swap contracts.

    * F17 — never ``.detach()`` anywhere outside of comments.
    * F22 — overrides on the new instance only carry design-intent props
      (variants, text, sizing). The template does not call setBoundVariable
      or set fills/strokes at swap time; binding work runs separately.
    * F30 — no terminal throw on aggregate stats; only init-time throws
      (missing target page, missing idMap) are allowed.
    """
    rows = [
        SwapRow(src="a", newKey="b", variants={"Type": "Logo"}),
        SwapRow(src="c", newKey="d"),
    ]
    js = render_swap_script(page_node_id="9559:29", namespace="ns", rows=rows)
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    # F17
    assert not re.search(r"\.detach\s*\(", js_no_comments), (
        "F17 violated: emitted swap JS contains a .detach() call"
    )
    # F22 — no boundVariable / fills writes inside the swap template.
    assert "setBoundVariable" not in js_no_comments
    assert "setBoundVariableForPaint" not in js_no_comments
    # F30
    init_throws = re.findall(r"throw new Error\(`[^`]+`\)", js_no_comments)
    for thrown in init_throws:
        assert "target page not found" in thrown or "missing idMap" in thrown, (
            f"swap JS contains a non-init throw: {thrown!r}"
        )
    # Core swap operations must be present.
    assert "importComponentSetByKeyAsync" in js_no_comments
    assert "createInstance" in js_no_comments
    assert "insertChild" in js_no_comments
    assert "oldInstance.remove()" in js_no_comments


def test_issue_162_swap_js_persists_idmap_for_followup_apply_tokens() -> None:
    """The script writes back the merged idMap so apply-tokens hits NEW ids."""
    js = render_swap_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[SwapRow(src="a", newKey="b")],
    )
    assert 'writeSPDChunks("idMap", "idMapChunkCount"' in js
    assert "newIdMapAdditions[row.src] = newInstance.id;" in js


def test_issue_162_swap_dry_run_reports_unique_new_keys_and_old_cids(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "swap.json"
    manifest.write_text(
        json.dumps(
            [
                {"src": "1", "oldCid": "OLD1", "newKey": "K1", "variants": {"X": "y"}},
                {"src": "2", "oldCid": "OLD1", "newKey": "K1", "variants": {"X": "y"}},
                {"src": "3", "oldCid": "OLD2", "newKey": "K2", "variants": {"X": "y"}},
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

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["mode"] == "dry-run"
    assert data["rows"] == 3
    assert data["unique_new_keys"] == 2
    assert data["unique_old_component_ids"] == 2
    assert data["by_new_key"] == {"K1": 2, "K2": 1}


def test_issue_162_swap_emit_only_writes_deterministic_batches(tmp_path: Path) -> None:
    manifest = tmp_path / "swap.json"
    manifest.write_text(
        json.dumps([{"src": str(i), "newKey": "K", "variants": {"X": "y"}} for i in range(3)]),
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
            "--batch-size",
            "2",
            "--batch-dir",
            "swap_batches",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    batch_dir = tmp_path / "swap_batches"
    batch_manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert batch_manifest["total_rows"] == 3
    assert batch_manifest["batch_count"] == 2
    first = json.loads((batch_dir / "swap-batch-0001.json").read_text(encoding="utf-8"))
    assert len(first) == 2
    js = (batch_dir / "swap-batch-0001.use_figma.js").read_text(encoding="utf-8")
    assert "Generated by figmaclaw audit-page swap" in js
    assert 'const TARGET_PAGE_ID = "9559:29";' in js


def test_issue_162_swap_refuses_when_manifest_disagrees_with_cli_scope(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "swap.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "figmaclaw.audit_page_swap.manifest",
                "file_key": "OTHER",
                "page_node_id": "9559:29",
                "rows": [{"src": "a", "newKey": "b"}],
            }
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

    assert result.exit_code == 2
    assert "manifest file_key" in result.output


# Issue #163 — variant-axis lint --------------------------------------------


def test_issue_163_parse_variant_axis_assignment_handles_both_shapes() -> None:
    assert parse_variant_axis_assignment("color=primary, style=filled") == {
        "color": "primary",
        "style": "filled",
    }
    assert parse_variant_axis_assignment({"color": "primary"}) == {"color": "primary"}
    assert parse_variant_axis_assignment("garbage") == {}


def test_issue_163_lint_flags_unknown_variant_axis_value() -> None:
    """The Login Method button bug: type=email is not a published value."""
    rule = parse_flat_rule(
        {
            "old_component_set": "Login Method button",
            "old_key": "old3",
            "new_component_set": "button-social",
            "new_key": "new3",
            "swap_strategy": "direct",
            "variant_mapping": {"Email": "type=email", "Phone": "type=phone"},
        }
    )
    doc = VariantTaxonomyDocument.model_validate(
        {
            "new3": {
                "name": "button-social",
                "axes": {"type": {"values": ["google", "apple", "fb", "x"]}},
            }
        }
    )
    findings = validate_rule_variant_mapping(rule, 0, doc.component_sets)
    msgs = [f.message for f in findings]
    assert any("'email'" in (m or "") and "type" in (m or "") for m in msgs)
    assert any("'phone'" in (m or "") and "type" in (m or "") for m in msgs)


def test_issue_163_lint_flags_old_axis_coverage_gap() -> None:
    """Buttons Desktop must cover Quaternary and Quinary; the user's draft did not."""
    rule = parse_flat_rule(
        {
            "old_component_set": "Buttons Desktop",
            "old_key": "old2",
            "new_component_set": "button",
            "new_key": "new2",
            "swap_strategy": "direct",
            "variant_mapping": {
                "Primary": "color=primary, style=filled",
                "Secondary": "color=secondary, style=filled",
                "Tertiary": "color=primary, style=plain",
            },
        }
    )
    doc = VariantTaxonomyDocument.model_validate(
        {
            "new2": {
                "axes": {
                    "color": {"values": ["primary", "secondary", "inverted", "danger"]},
                    "style": {"values": ["filled", "tinted", "plain"]},
                }
            },
            "old2": {
                "axes": {
                    "Hierarchy": {
                        "values": [
                            "Primary",
                            "Secondary",
                            "Tertiary",
                            "Quaternary",
                            "Quinary",
                        ]
                    }
                }
            },
        }
    )
    findings = validate_rule_variant_mapping(rule, 0, doc.component_sets)
    msgs = [f.message for f in findings]
    assert any("Quaternary" in (m or "") and "Quinary" in (m or "") for m in msgs)


def test_issue_163_lint_passes_fixed_assignment_when_values_published() -> None:
    """Brand Logo {Type: Logo, Colored: True} is valid; lint must not fire."""
    rule = parse_flat_rule(
        {
            "old_component_set": "logo",
            "old_key": "old1",
            "new_component_set": "Brand Logo",
            "new_key": "new1",
            "swap_strategy": "direct",
            "variant_mapping": {"Type": "Logo", "Colored": "True"},
        }
    )
    doc = VariantTaxonomyDocument.model_validate(
        {
            "new1": {
                "axes": {
                    "Type": {"values": ["Logo", "Mono", "On Shape"]},
                    "Colored": {"values": ["True", "False"]},
                }
            }
        }
    )
    findings = validate_rule_variant_mapping(rule, 0, doc.component_sets)
    assert findings == []


def test_issue_163_lint_warns_when_no_taxonomy_provided() -> None:
    """Without --variants the lint flags that the check was skipped."""
    rule = parse_flat_rule(
        {
            "old_key": "old1",
            "new_key": "new1",
            "swap_strategy": "direct",
            "variant_mapping": {"X": "a"},
        }
    )
    findings = validate_rule_variant_mapping(rule, 0, {})
    assert findings and findings[0].status == "warning"
    assert "--variants" in (findings[0].message or "")


# Issue #164 — v3-flat schema acceptance ------------------------------------


def test_issue_164_lint_accepts_v3_flat_direct_rule(tmp_path: Path) -> None:
    """The v3-flat shape from the user's repro must lint cleanly without
    requiring the v3-nested ``target`` block."""
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rules": [
                    {
                        "old_component_set": "logo",
                        "old_key": "a81dce8bb75eb3f06ba8749ef0ad71c6a34b54a6",
                        "old_componentId_examples": ["8009:29"],
                        "new_component_set": "Brand Logo",
                        "new_key": "e81fbd3e7c55508994f4630923b16d61f349eabf",
                        "confidence": "needs_variant_validation",
                        "swap_strategy": "direct",
                        "variant_mapping": {"Type": "Logo", "Colored": "True"},
                        "preserve": ["size"],
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

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["rule_count"] == 1
    # No "missing target/parent_handling/property_translation/validation"
    # errors — that's the issue #164 regression we are protecting against.
    error_messages = [f["message"] for f in data["findings"] if f["status"] == "error"]
    for forbidden in (
        "missing target",
        "missing parent_handling",
        "missing property_translation",
        "missing validation",
    ):
        assert all(forbidden not in (m or "") for m in error_messages), (
            f"v3-flat rule should not fire {forbidden!r}; got {error_messages}"
        )


def test_issue_164_lint_validates_recompose_local_requires_recomposition_plan(
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
                        "old_key": "tk",
                        "swap_strategy": "recompose_local",
                        # recomposition_plan deliberately missing
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
    assert data["ok"] is False
    assert any(
        "recomposition_plan" in (f["message"] or "")
        for f in data["findings"]
        if f["status"] == "error"
    )


def test_issue_164_lint_validates_audit_only_requires_audit_kind(
    tmp_path: Path,
) -> None:
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rules": [
                    {
                        "old_component_set": "Foo",
                        "old_key": "fk",
                        "swap_strategy": "audit_only",
                        "audit_required": True,
                        # audit_kind deliberately missing
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
    assert data["ok"] is False
    assert any(
        "audit_kind" in (f["message"] or "") for f in data["findings"] if f["status"] == "error"
    )


def test_issue_164_nested_v3_rule_still_validated(tmp_path: Path) -> None:
    """Backwards-compat: existing v3-nested rules must keep working."""
    component_map = tmp_path / "component_migration_map.v3.json"
    component_map.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rules": [
                    {
                        "old_component_set": "Button",
                        "old_key": "old-key",
                        "target": {
                            "status": "replace_with_new_component",
                            "new_key": "new-key",
                            "expected_type": "COMPONENT_SET",
                            "expected_new_name": "button",
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
    assert data["rule_count"] == 1
    error_msgs = [f["message"] for f in data["findings"] if f["status"] == "error"]
    assert error_msgs == []


# Issue #165 — compact-row refusal mentions unrecognised fields --------------


def _make_catalog_repo(tmp_path: Path) -> None:
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


def test_issue_165_compact_row_refusal_lists_unrecognised_field_names(
    tmp_path: Path,
) -> None:
    """The wrong-shape row from the repro: {node_id, prop, var_name}."""
    _make_catalog_repo(tmp_path)
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
    data = json.loads(result.output)
    assert data["fixes"] == 0
    assert data["refusals"] == 1
    assert data["counts"]["refusals"].get("unrecognised_compact_row_fields") == 1

    remaining = json.loads((tmp_path / "remaining.json").read_text(encoding="utf-8"))
    refusals = remaining.get("refusals") or remaining.get("rows") or remaining
    # The refusal payload must list the unrecognised keys explicitly.
    flat = json.dumps(refusals)
    assert "prop" in flat and "var_name" in flat
    assert "unrecognised_compact_row_fields" in flat


def test_issue_165_canonical_compact_row_still_resolves(tmp_path: Path) -> None:
    """The accepted shape ({n,p,t} or {node_id,property,token_name}) keeps working."""
    _make_catalog_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "fg/inverse"}]),
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
    assert data["fixes"] == 1
    assert data["refusals"] == 0


# Issue #166 — non-active page name detection ------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "🛠 Audit — Web App page 2026-05-07",
        "📦 Archive",
        "📦 ARCHIVE",
        "🚧 IN PROGRESS - Mobile",
        "😎 PLAYGROUND",
        "Mobile Playground",
        "[OLD] Components",
        "[Archived 17 Nov 2025]",
        "Gigaverse UX UI ARCHIVE 🚫",
        "Giles - Giga Wires WIP",
        "Hackaton - Anymous user",
        "claude test / Page 1",
        "Untitled UI – PRO STYLES (v7.0) (Copy)",
        "Monetization Archive",
        "[wip] [process] Design versioning",
        "Base [Nope, not anymore] 👋",
        "Delete",
    ],
)
def test_issue_166_inactive_page_names_detected(name: str) -> None:
    assert looks_like_inactive_page_name(name), (
        f"{name!r} should be flagged as a non-active page name"
    )


@pytest.mark.parametrize(
    "name",
    [
        "✅ Web App",
        "✅ Mobile App",
        "❖ Design System",
        "Community / Homepage MOBILE",
        "Branding / round 5 - everything is a conversation",
        "Page 1",
        "🏞️ Cover",
        "Threaded replies",
        "Auditor's report",  # word "audit" must not match inside "Auditor"
    ],
)
def test_issue_166_active_page_names_not_flagged(name: str) -> None:
    assert not looks_like_inactive_page_name(name), (
        f"{name!r} should not be flagged — it does not match an inactive marker"
    )


def test_issue_166_emit_clone_script_warns_on_inactive_source(
    tmp_path: Path,
) -> None:
    fake = MagicMock()
    fake.get_nodes = AsyncMock(
        return_value={
            "9451:29": {
                "id": "9451:29",
                "name": "🛠 Audit — Web App page 2026-05-07",
                "type": "CANVAS",
            }
        }
    )
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    with patch("figmaclaw.commands.audit_page.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "audit-page",
                "emit-clone-script",
                "FILE",
                "9451:29",
                "--out",
                "-",
            ],
            catch_exceptions=False,
            env={"FIGMA_API_KEY": "x"},
        )
    assert result.exit_code == 0
    assert "non-active page" in result.output or "WARNING" in result.output


def test_issue_166_emit_clone_script_strict_source_refuses(
    tmp_path: Path,
) -> None:
    fake = MagicMock()
    fake.get_nodes = AsyncMock(
        return_value={"9451:29": {"id": "9451:29", "name": "📦 Archive", "type": "CANVAS"}}
    )
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    with patch("figmaclaw.commands.audit_page.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "audit-page",
                "emit-clone-script",
                "FILE",
                "9451:29",
                "--out",
                "-",
                "--strict-source",
            ],
            catch_exceptions=False,
            env={"FIGMA_API_KEY": "x"},
        )
    assert result.exit_code != 0


def test_issue_166_allow_audit_page_source_suppresses_warning(
    tmp_path: Path,
) -> None:
    fake = MagicMock()
    fake.get_nodes = AsyncMock(
        return_value={"9451:29": {"id": "9451:29", "name": "📦 Archive", "type": "CANVAS"}}
    )
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    with patch("figmaclaw.commands.audit_page.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "audit-page",
                "emit-clone-script",
                "FILE",
                "9451:29",
                "--out",
                "-",
                "--strict-source",
                "--allow-audit-page-source",
            ],
            catch_exceptions=False,
            env={"FIGMA_API_KEY": "x"},
        )
    assert result.exit_code == 0
    assert "non-active page" not in result.output


# Pydantic model invariants --------------------------------------------------


def test_flat_rule_discriminates_on_swap_strategy() -> None:
    direct = parse_flat_rule(
        {
            "old_key": "ok",
            "swap_strategy": "direct",
            "new_key": "nk",
            "variant_mapping": {"X": "y"},
        }
    )
    assert isinstance(direct, FlatDirectRule)


def test_nested_rule_replace_requires_new_key() -> None:
    with pytest.raises(ValidationError):
        NestedRule.model_validate(
            {
                "old_component_set": "Button",
                "old_key": "old-key",
                "target": {
                    "status": "replace_with_new_component",
                    "expected_type": "COMPONENT_SET",
                    # new_key + expected_new_name omitted on purpose
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
