"""Invariant tests for issues #168 (F41 import-by-key fallback) and
#169 (F48 class-level signature abort).

Each test names the user-visible invariant it protects, not the bug it
fixes. The two issues land together because both extend the same JS
template's runtime behaviour.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from figmaclaw.apply_tokens import (
    APPLY_TOKENS_JS_TEMPLATE,
    DEFAULT_SIGNATURE_ABORT_THRESHOLD,
    EXIT_OPERATOR_ACTION_REQUIRED,
    _catalog_key_by_token_name,
    operator_action_for_signature,
    render_apply_tokens_script,
)
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli
from figmaclaw.token_catalog import TokenCatalog


def _make_repo(tmp_path: Path, *, with_key: bool = True) -> None:
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
    var: dict = {
        "library_hash": "libabc",
        "collection_id": "c1",
        "name": "bg/neutral/inverse",
        "resolved_type": "COLOR",
        "values_by_mode": {"light": {"hex": "#FFF"}},
        "source": "figma_api",
    }
    if with_key:
        var["key"] = "bg-neutral-inverse-key"
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
                "variables": {"VariableID:libabc/1:1": var},
            }
        ),
        encoding="utf-8",
    )


def _load_catalog(tmp_path: Path) -> TokenCatalog:
    payload = (tmp_path / ".figma-sync" / "ds_catalog.json").read_text(encoding="utf-8")
    return TokenCatalog.model_validate_json(payload)


# Issue #168 / F41 — import-by-key fallback ----------------------------------


def test_issue_168_catalog_key_by_token_name_indexes_authoritative_keys(
    tmp_path: Path,
) -> None:
    """Helper produces a token-name → key map for authoritative entries only."""
    _make_repo(tmp_path)
    catalog = _load_catalog(tmp_path)
    name_map = _catalog_key_by_token_name(catalog)
    assert name_map == {"bg/neutral/inverse": "bg-neutral-inverse-key"}


def test_issue_168_emitted_js_carries_catalog_key_map(tmp_path: Path) -> None:
    """The runtime needs the name→key map at hand to do the fallback."""
    js = render_apply_tokens_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[],
        node_map="shared-plugin-data",
        catalog_key_by_token_name={"bg/neutral/inverse": "k1"},
    )
    assert "CATALOG_KEY_BY_TOKEN_NAME" in js
    # The JSON literal lands as a const so the runtime can read it.
    assert '"bg/neutral/inverse":"k1"' in js


def test_issue_168_runtime_falls_back_to_import_by_token_name(tmp_path: Path) -> None:
    """INVARIANT (F41): when both variable_id and variable_key fail, the
    runtime must try `importVariableByKeyAsync(catalog_key_by_name[token])`
    before giving up. We test by inspecting the rendered JS for the
    fallback chain — three consecutive try blocks ending in the catalog
    map lookup.
    """
    js = render_apply_tokens_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[],
        node_map="shared-plugin-data",
        catalog_key_by_token_name={"bg/neutral/inverse": "bg-neutral-inverse-key"},
    )
    # Strip comments; the fallback chain is real code, not a doc string.
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    # First: row.variable_key → importVariableByKeyAsync.
    assert "row.variable_key" in js_no_comments
    assert "importVariableByKeyAsync(row.variable_key)" in js_no_comments
    # Second: row.variable_id → getVariableByIdAsync.
    assert "getVariableByIdAsync(row.variable_id)" in js_no_comments
    # Third (the new F41 fallback): token_name → catalog map → import.
    assert "CATALOG_KEY_BY_TOKEN_NAME[row.token_name]" in js_no_comments
    assert "importVariableByKeyAsync(catalogKey)" in js_no_comments


def test_issue_168_apply_tokens_emit_only_includes_catalog_map(
    tmp_path: Path,
) -> None:
    """End-to-end: emit-only mode bakes the catalog map into every batch JS."""
    _make_repo(tmp_path)
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
    assert result.exit_code == 0, result.output
    js = (tmp_path / "apply_batches" / "batch-0001.use_figma.js").read_text(encoding="utf-8")
    assert '"bg/neutral/inverse":"bg-neutral-inverse-key"' in js


# Issue #169 / F48 — class-level signature abort -----------------------------


def test_issue_169_emitted_js_aggregates_errors_by_signature() -> None:
    """The runtime collects errors into errorSignatures keyed by class."""
    js = render_apply_tokens_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[],
        node_map="shared-plugin-data",
    )
    # The signature aggregator + classifier are present.
    assert "errorSignatures" in js
    assert "recordSignature" in js
    assert "classifyError" in js
    # And the canonical signature classes are wired in.
    assert "unloadable_font:" in js
    assert "read_only_file" in js
    assert "missing_variable_key" in js
    assert "variable_not_found" in js


def test_issue_169_threshold_default_is_five_and_overridable() -> None:
    """Default threshold matches the spec; the renderer accepts overrides."""
    assert DEFAULT_SIGNATURE_ABORT_THRESHOLD == 5
    js = render_apply_tokens_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[],
        node_map="shared-plugin-data",
        signature_abort_threshold=12,
    )
    assert "const SIGNATURE_ABORT_THRESHOLD = 12;" in js


def test_issue_169_summary_carries_signature_abort_field() -> None:
    """The runtime returns a `signatureAbort` field for the CLI to read."""
    assert "signatureAbort" in APPLY_TOKENS_JS_TEMPLATE
    assert "stats.aborted_by_signature" in APPLY_TOKENS_JS_TEMPLATE


def test_issue_169_runtime_loop_skips_remaining_rows_after_abort() -> None:
    """Once signatureAbort is set, the per-row loop short-circuits.

    Without this, the count would keep climbing for the same signature
    after the operator-action threshold has fired, defeating the point of
    aborting "early enough that the remaining rows aren't wasted work".
    """
    js = render_apply_tokens_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[],
        node_map="shared-plugin-data",
    )
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    assert "if (signatureAbort)" in js_no_comments
    # And aborted rows are counted separately from real per-row failures
    # so the report doesn't confuse "skipped after abort" with "errored".
    assert "stats.aborted_by_signature++" in js_no_comments


@pytest.mark.parametrize(
    "signature, must_contain",
    [
        ("unloadable_font:Boldonse Bold", "Boldonse Bold"),
        ("unloadable_font:Boldonse Bold", "org-upload"),
        ("read_only_file", "Editor"),
        ("missing_variable_key", "publishable key"),
        ("variable_not_found:bg/neutral/inverse", "bg/neutral/inverse"),
    ],
)
def test_issue_169_operator_action_text_includes_class_specific_hint(
    signature: str, must_contain: str
) -> None:
    """Each known signature class produces an operator-actionable instruction.

    F36 says these go FIRST, TERSE, with the exact fix. We assert per-class
    that the canonical user-blocking concept is mentioned (Boldonse for
    the font case, Editor permission for read-only, etc.).
    """
    text = operator_action_for_signature(signature)
    assert text, f"signature {signature!r} must produce a non-empty hint"
    assert must_contain in text, (
        f"hint for {signature!r} must mention {must_contain!r}; got: {text}"
    )


def test_issue_169_unknown_signature_returns_empty_hint() -> None:
    """An unrecognised signature returns "" so the CLI can fall back to a
    generic message rather than fabricating advice."""
    assert operator_action_for_signature("freaky_new_class:foo") == ""


def test_issue_169_cli_exit_code_on_signature_abort(tmp_path: Path) -> None:
    """When a batch returns signatureAbort, the CLI exits with EXIT_OPERATOR_ACTION_REQUIRED.

    We patch `execute_use_figma_calls` to fake the JS-runtime return shape
    so the test stays unit-fast and doesn't need a real Figma session.
    """
    _make_repo(tmp_path)
    rows_path = tmp_path / "bindings_for_figma.json"
    rows_path.write_text(
        json.dumps([{"n": "1:2", "p": "fill", "t": "bg/neutral/inverse"}]),
        encoding="utf-8",
    )

    fake_execution = {
        "ok": False,
        "batches": [
            {
                "result": {
                    "ok": False,
                    "stats": {"applied": 0, "errors": 5},
                    "signatureAbort": {
                        "signature": "unloadable_font:Boldonse Bold",
                        "count": 5,
                        "sample_rows": ["1:2", "1:3", "1:4"],
                    },
                }
            }
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

    assert result.exit_code == EXIT_OPERATOR_ACTION_REQUIRED, result.output
    # The F36 block is on stderr in the CLI; CliRunner captures both into
    # `output` by default. Look for the canonical leading marker.
    assert "ACTION REQUIRED" in result.output
    assert "Boldonse Bold" in result.output
