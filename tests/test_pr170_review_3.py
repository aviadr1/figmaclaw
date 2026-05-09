"""Tests for the fifth-round PR #170 review fixes.

Each test names the finding it protects against. See the fifth review
report for the full list. This round extended the dev/operator surface:
robust _safe_count, dedup of same-signature aborts, warn-and-skip
catalog conflicts (issue #171), F41 observability counters, and clean
CLI handling of unknown signatures.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from figmaclaw.apply_tokens import (
    _catalog_key_by_token_name,
    render_apply_tokens_script,
)
from figmaclaw.commands.apply_tokens import (
    _GENERIC_OPERATOR_ACTION_FALLBACK,
    _merge_aborts_by_signature,
    _safe_count,
)
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli
from figmaclaw.token_catalog import TokenCatalog


def _mcp_call_record(structured: dict) -> dict:
    return {
        "index": 1,
        "description": "batch",
        "isError": False,
        "result": {"structuredContent": structured},
    }


def _make_repo(tmp_path: Path, *, with_extra_dup: bool = False) -> None:
    state = FigmaSyncState(tmp_path)
    state.add_tracked_file("file123", "DS")
    state.set_file_meta(
        "file123",
        version="v2",
        last_modified="2026-05-09T00:00:00Z",
        last_checked_at="2026-05-09T00:00:00Z",
        file_name="DS",
    )
    state.save()
    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    variables = {
        "VariableID:libabc/1:1": {
            "library_hash": "libabc",
            "collection_id": "c1",
            "name": "bg/neutral/inverse",
            "key": "bg-neutral-inverse-key",
            "resolved_type": "COLOR",
            "values_by_mode": {"light": {"hex": "#FFF"}},
            "source": "figma_api",
        }
    }
    if with_extra_dup:
        # Two distinct keys for the same token name — the #171 trigger.
        variables["VariableID:libabc/2:1"] = {
            "library_hash": "libabc",
            "collection_id": "c1",
            "name": "bg/neutral/surface-subtle",
            "key": "key-a",
            "resolved_type": "COLOR",
            "values_by_mode": {"light": {"hex": "#FFF"}},
            "source": "figma_api",
        }
        variables["VariableID:libabc/2:2"] = {
            "library_hash": "libabc",
            "collection_id": "c1",
            "name": "bg/neutral/surface-subtle",
            "key": "key-b",
            "resolved_type": "COLOR",
            "values_by_mode": {"light": {"hex": "#000"}},
            "source": "figma_api",
        }
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
                "variables": variables,
            }
        ),
        encoding="utf-8",
    )


# Finding #1 — _safe_count handles inf + OverflowError -------------------


@pytest.mark.parametrize(
    "raw",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        "infinity",
        "Infinity",
        "-inf",
        "NaN",
    ],
)
def test_review5_finding_1_safe_count_returns_zero_for_non_finite(raw) -> None:
    """The defensive coercion must not crash on non-finite inputs.

    The earlier helper caught (TypeError, ValueError) but `int(inf)`
    raises `OverflowError`, which would propagate from the abort
    surface — the very surface meant to be defensive.
    """
    assert _safe_count(raw) == 0


# Finding #2 — same-signature aborts dedupe across batches --------------


def test_review5_finding_2_merge_aborts_collapses_duplicate_signatures() -> None:
    """Two batches each aborting on the same signature must produce ONE
    merged entry — not a primary + an additional_signatures echo.

    Operator-facing: the wall-of-noise the F48 surface fixed would
    re-appear as "primary X hit N; also seen X hit M".
    """
    aborts = [
        {
            "signature": "unloadable_font:Boldonse Bold",
            "count": 263,
            "sample_rows": ["a:1", "a:2", "a:3"],
        },
        {
            "signature": "unloadable_font:Boldonse Bold",
            "count": 47,
            "sample_rows": ["b:1", "b:2"],
        },
    ]
    merged = _merge_aborts_by_signature(aborts)
    assert len(merged) == 1
    assert merged[0]["signature"] == "unloadable_font:Boldonse Bold"
    assert merged[0]["count"] == 310  # 263 + 47
    # Sample rows union, preserved first-seen order, capped at 3.
    assert merged[0]["sample_rows"] == ["a:1", "a:2", "a:3"]


def test_review5_finding_2_merge_aborts_keeps_distinct_signatures_separate() -> None:
    aborts = [
        {"signature": "unloadable_font:A", "count": 5, "sample_rows": ["a"]},
        {"signature": "read_only_file", "count": 5, "sample_rows": ["b"]},
        {"signature": "unloadable_font:A", "count": 3, "sample_rows": ["a2"]},
    ]
    merged = _merge_aborts_by_signature(aborts)
    by_sig = {a["signature"]: a for a in merged}
    assert by_sig["unloadable_font:A"]["count"] == 8
    assert by_sig["unloadable_font:A"]["sample_rows"] == ["a", "a2"]
    assert by_sig["read_only_file"]["count"] == 5


def test_review5_finding_2_cli_does_not_echo_same_signature_twice(
    tmp_path: Path,
) -> None:
    """End-to-end: when batches share an abort signature, the CLI's
    ACTION REQUIRED + 'also seen' lines do NOT repeat that signature.
    """
    _make_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "bg/neutral/inverse"}]),
        encoding="utf-8",
    )
    fake_execution = {
        "mode": "execute",
        "calls": [
            _mcp_call_record(
                {
                    "signatureAbort": {
                        "signature": "unloadable_font:Boldonse Bold",
                        "count": 263,
                        "sample_rows": ["a"],
                    }
                }
            ),
            _mcp_call_record(
                {
                    "signatureAbort": {
                        "signature": "unloadable_font:Boldonse Bold",
                        "count": 47,
                        "sample_rows": ["b"],
                    }
                }
            ),
        ],
    }

    async def fake_execute(*_args, **_kwargs):
        return fake_execution

    with patch("figmaclaw.commands.apply_tokens.execute_use_figma_calls", side_effect=fake_execute):
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
                "--execute",
                "--batch-dir",
                "apply_batches",
            ],
            catch_exceptions=False,
        )
    # No "also seen:" line should mention the dominant signature
    # (the merge collapsed both batches into one entry).
    also_seen_lines = [line for line in result.output.splitlines() if "also seen:" in line]
    assert all("unloadable_font:Boldonse Bold" not in line for line in also_seen_lines), (
        f"primary signature must not echo in 'also seen': {also_seen_lines}"
    )
    # And the merged count is the sum (263 + 47 = 310) on the ACTION
    # REQUIRED line.
    action_line = next(line for line in result.output.splitlines() if "ACTION REQUIRED" in line)
    assert "310 time(s)" in action_line


# Finding #3 / #171 — warn-and-skip catalog dup; CLI catches ValueError ---


def test_review5_finding_3_unreferenced_catalog_dup_does_not_abort_run(
    tmp_path: Path,
) -> None:
    """Issue #171: a catalog with two keys for the same token name must
    NOT abort runs whose manifest doesn't reference that token.

    The conflicting name simply gets skipped from the F41 map; rows
    that reference it would fall through to the legacy variable_id
    path. Operators see the conflicts via the manifest's
    `catalog_name_conflicts` field.
    """
    _make_repo(tmp_path, with_extra_dup=True)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "bg/neutral/inverse"}]),
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
            "--emit-only",
            "--batch-dir",
            "apply_batches",
        ],
        catch_exceptions=False,
    )
    # Run succeeds (exit 0) — the unreferenced conflict no longer aborts.
    assert result.exit_code == 0, result.output
    # Operator sees a warning on stderr.
    assert "ambiguous token name" in result.output
    # Batch manifest carries the conflicts.
    manifest_path = tmp_path / "apply_batches" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "catalog_name_conflicts" in manifest
    conflicts = manifest["catalog_name_conflicts"]
    assert "bg/neutral/surface-subtle" in conflicts
    assert sorted(conflicts["bg/neutral/surface-subtle"]) == ["key-a", "key-b"]


# Finding #4 — empty-instruction fallback in CLI ACTION REQUIRED ------


def test_review5_finding_4_unknown_signature_uses_generic_fallback_hint(
    tmp_path: Path,
) -> None:
    """An unknown / future signature class produces a generic hint
    rather than a trailing space in the ACTION REQUIRED line.
    """
    _make_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "bg/neutral/inverse"}]),
        encoding="utf-8",
    )
    fake_execution = {
        "mode": "execute",
        "calls": [
            _mcp_call_record(
                {
                    "signatureAbort": {
                        "signature": "newly_invented_class:foo",
                        "count": 5,
                        "sample_rows": ["x"],
                    }
                }
            )
        ],
    }

    async def fake_execute(*_args, **_kwargs):
        return fake_execution

    with patch("figmaclaw.commands.apply_tokens.execute_use_figma_calls", side_effect=fake_execute):
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
                "--execute",
                "--batch-dir",
                "apply_batches",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code != 0
    # The fallback text appears in the ACTION REQUIRED line, NOT a bare
    # "aborting." with trailing space.
    assert _GENERIC_OPERATOR_ACTION_FALLBACK in result.output
    assert "aborting. \n" not in result.output


# Finding #5 — F41 fallback observability counters --------------------


def test_review5_finding_5_stats_carries_resolution_kind_counters() -> None:
    """The JS template's stats object carries per-kind resolution counters
    so operators can see when F41 fired (vs. when rows already had keys).
    """
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    # All three counters initialised to 0 in stats.
    assert "resolved_via_variable_key: 0" in js
    assert "resolved_via_variable_id: 0" in js
    assert "resolved_via_catalog_fallback: 0" in js
    # The recordResolveOutcome helper increments them per kind.
    assert "recordResolveOutcome" in js
    # And it's called in the resolver-success path.
    assert "recordResolveOutcome(outcome.kind)" in js


# Finding #6 — docstring naming aligned ---------------------------------


def test_review5_finding_6_docstring_uses_real_constant_name() -> None:
    from figmaclaw.apply_tokens import write_apply_batches

    docstring = write_apply_batches.__doc__ or ""
    assert "CATALOG_KEY_BY_TOKEN_NAME" in docstring
    # Old name no longer appears.
    assert "dsCatalogKeyByName" not in docstring


# Smoke: previously-passing tests still pass with new tuple return shape.


def test_review5_catalog_key_by_token_name_returns_tuple() -> None:
    """API-shape assertion: the helper now returns (name_map, conflicts).
    Callers must unpack — guards against accidental dict-only consumers.
    """
    catalog = TokenCatalog.model_validate(
        {
            "schema_version": 2,
            "libraries": {"lib1": {"name": "L1", "source": "figma_api"}},
            "variables": {
                "VariableID:lib1/1:1": {
                    "library_hash": "lib1",
                    "collection_id": "c1",
                    "name": "fg/inverse",
                    "key": "k1",
                    "resolved_type": "COLOR",
                    "values_by_mode": {"light": {"hex": "#FFF"}},
                    "source": "figma_api",
                }
            },
        }
    )
    result = _catalog_key_by_token_name(catalog)
    assert isinstance(result, tuple) and len(result) == 2
    name_map, conflicts = result
    assert name_map == {"fg/inverse": "k1"}
    assert conflicts == {}
