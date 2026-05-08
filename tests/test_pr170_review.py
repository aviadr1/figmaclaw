"""Tests for PR #170 review findings.

Each test names the finding it protects against. The agent's review of
PR #170 uncovered a critical bug (the F48 abort surface walked the wrong
key in the executor result) plus signature-drift and DRY items. This
file pins the user-visible invariants every fix protects so a regression
surfaces in CI rather than at use_figma runtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from figmaclaw.apply_tokens import (
    OperatorAction,
    _catalog_key_by_token_name,
    operator_action_for_signature,
    render_apply_tokens_script,
)
from figmaclaw.commands.apply_tokens import _collect_signature_aborts
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli
from figmaclaw.token_catalog import TokenCatalog

# Finding #1 (critical) — _collect_signature_aborts walks real MCP shape ----


def _mcp_call_record(structured: dict) -> dict:
    """Real-shape entry from execute_use_figma_calls: each call carries an
    MCP tools/call result with the JS summary in `structuredContent`.
    """
    return {
        "index": 1,
        "description": "apply design token bindings batch 0001",
        "isError": False,
        "result": {"structuredContent": structured},
    }


def _mcp_call_record_text(structured: dict) -> dict:
    """Same as above but the JS summary lives in `content[0].text` as JSON."""
    return {
        "index": 1,
        "description": "batch",
        "isError": False,
        "result": {"content": [{"type": "text", "text": json.dumps(structured)}]},
    }


def test_pr170_finding_1_collect_aborts_reads_structured_content() -> None:
    aborts = _collect_signature_aborts(
        {
            "calls": [
                _mcp_call_record(
                    {
                        "ok": False,
                        "signatureAbort": {
                            "signature": "unloadable_font:Boldonse Bold",
                            "count": 5,
                            "sample_rows": ["1:2"],
                        },
                    }
                )
            ]
        }
    )
    assert len(aborts) == 1
    assert aborts[0]["signature"] == "unloadable_font:Boldonse Bold"


def test_pr170_finding_1_collect_aborts_reads_text_content() -> None:
    """Some MCP servers return the JS result as JSON-stringified text rather
    than `structuredContent`; the walker must handle both."""
    aborts = _collect_signature_aborts(
        {
            "calls": [
                _mcp_call_record_text(
                    {
                        "signatureAbort": {
                            "signature": "read_only_file",
                            "count": 5,
                            "sample_rows": [],
                        }
                    }
                )
            ]
        }
    )
    assert aborts and aborts[0]["signature"] == "read_only_file"


def test_pr170_finding_1_collect_aborts_ignores_old_batches_key() -> None:
    """Sanity: the old `batches` key (which never existed in the executor
    output) doesn't accidentally still work via some legacy fallback."""
    # Executor never returns this shape; verify we don't fish from it.
    bogus = {"batches": [{"result": {"signatureAbort": {"signature": "x", "count": 5}}}]}
    assert _collect_signature_aborts(bogus) == []


# Finding #2 — classifyError ↔ operator_action_for_signature alignment ------


def test_pr170_finding_2_unloadable_font_full_class_identifier_format() -> None:
    """The JS template emits `unloadable_font:<name>` with the colon-suffixed
    identifier. The Python operator-action helper must recognise it."""
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    # The classifier is encoded in the JS source — verify the colon-prefix
    # form is what gets emitted (`"unloadable_font:" + m[1]`).
    assert '"unloadable_font:" + ' in js


def test_pr170_finding_2_missing_variable_key_includes_id_when_known() -> None:
    """The classifier passes the row's variable_id into the signature so the
    F36 hint can name WHICH resolver entry needs a publishable key."""
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    # JS: `"missing_variable_key" + id` where id = ":" + row.variable_id
    assert '"missing_variable_key" + id' in js
    assert "contextRow && contextRow.variable_id" in js


@pytest.mark.parametrize(
    "signature, must_contain",
    [
        # Bare class — produces the legacy hint
        ("read_only_file", "Editor"),
        # Class:identifier shape produced by the new classifier
        ("missing_variable_key:VariableID:libabc/1:1", "VariableID:libabc/1:1"),
        ("variable_not_found:bg/neutral/inverse", "bg/neutral/inverse"),
        ("unloadable_font:Boldonse Bold", "Boldonse Bold"),
    ],
)
def test_pr170_finding_2_every_classifier_signature_has_a_hint(
    signature: str, must_contain: str
) -> None:
    """Every signature `classifyError` can emit must produce a non-empty
    operator-action hint — drift between the two would silently produce
    "ACTION REQUIRED — <sig> hit N times;" with no instruction."""
    text = operator_action_for_signature(signature)
    assert text, f"signature {signature!r} must produce a non-empty hint"
    assert must_contain in text, f"hint should mention {must_contain!r}; got: {text}"


def test_pr170_finding_2_split_on_first_colon_preserves_inner_colons() -> None:
    """`VariableID:libabc/1:1` is a real Figma id format with internal colons.
    The class-identifier split must use FIRST-colon partition so the
    identifier stays intact."""
    text = operator_action_for_signature("missing_variable_key:VariableID:libabc/1:1")
    assert "'VariableID:libabc/1:1'" in text


# Finding #3 — fallback chain ORDER (not just presence) ---------------------


def test_pr170_finding_3_resolver_candidates_are_in_required_order() -> None:
    """The F41 fallback must fire LAST (after both variable_key and
    variable_id miss). Inspect the rendered JS for the candidate-array
    construction and assert the kind values appear in order."""
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    # Find the indices of each candidate's `kind:` literal in source order.
    pos_key = js_no_comments.find('kind: "variable_key"')
    pos_id = js_no_comments.find('kind: "variable_id"')
    pos_catalog = js_no_comments.find('kind: "catalog_token_name"')
    assert pos_key > 0 and pos_id > 0 and pos_catalog > 0
    assert pos_key < pos_id < pos_catalog, (
        "candidate resolution order must be variable_key → variable_id → catalog "
        f"but found {pos_key=} {pos_id=} {pos_catalog=}"
    )


# Finding #5 — multi-batch abort surface ------------------------------------


def test_pr170_finding_5_collect_aborts_returns_every_batch_abort() -> None:
    """When multiple batches each abort on a different signature, the
    walker must return all of them — the CLI then sorts by count to
    surface the dominant one but still lists the rest."""
    aborts = _collect_signature_aborts(
        {
            "calls": [
                _mcp_call_record(
                    {
                        "signatureAbort": {
                            "signature": "unloadable_font:Boldonse Bold",
                            "count": 12,
                            "sample_rows": ["1:1"],
                        }
                    }
                ),
                _mcp_call_record(
                    {
                        "signatureAbort": {
                            "signature": "read_only_file",
                            "count": 5,
                            "sample_rows": [],
                        }
                    }
                ),
            ]
        }
    )
    sigs = {a["signature"] for a in aborts}
    assert sigs == {"unloadable_font:Boldonse Bold", "read_only_file"}


def test_pr170_finding_5_cli_lists_additional_signatures(tmp_path: Path) -> None:
    """The CLI human path lists every abort — the dominant one in the
    ACTION REQUIRED line, others under `also seen:` lines on stderr."""
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
                        "count": 12,
                        "sample_rows": ["1:1"],
                    }
                }
            ),
            _mcp_call_record(
                {
                    "signatureAbort": {
                        "signature": "read_only_file",
                        "count": 5,
                        "sample_rows": [],
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
    # Dominant signature wins the ACTION REQUIRED line; the other shows
    # up under "also seen:".
    assert "ACTION REQUIRED" in result.output
    assert "Boldonse Bold" in result.output
    assert "also seen: read_only_file" in result.output


# Finding #6 — duplicate token name detection -------------------------------


def _make_repo(tmp_path: Path) -> None:
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
                        "name": "bg/neutral/inverse",
                        "key": "key-from-tapin",
                        "resolved_type": "COLOR",
                        "values_by_mode": {"light": {"hex": "#FFF"}},
                        "source": "figma_api",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_pr170_finding_6_duplicate_token_name_distinct_keys_raises() -> None:
    """Two libraries publish the same token name with different keys ⇒ raise.

    Without this check, the runtime would silently pick the first key
    Python iterates and bind to the wrong variable. The error message
    has to name both libraries' keys so the operator can disambiguate
    via `--library`.
    """
    catalog = TokenCatalog.model_validate(
        {
            "schema_version": 2,
            "libraries": {
                "libold": {"name": "OLD", "source": "figma_api"},
                "libnew": {"name": "NEW", "source": "figma_api"},
            },
            "variables": {
                "VariableID:libold/1:1": {
                    "library_hash": "libold",
                    "collection_id": "c1",
                    "name": "fg/inverse",
                    "key": "old-key",
                    "resolved_type": "COLOR",
                    "values_by_mode": {"light": {"hex": "#000"}},
                    "source": "figma_api",
                },
                "VariableID:libnew/1:1": {
                    "library_hash": "libnew",
                    "collection_id": "c1",
                    "name": "fg/inverse",
                    "key": "new-key",
                    "resolved_type": "COLOR",
                    "values_by_mode": {"light": {"hex": "#FFF"}},
                    "source": "figma_api",
                },
            },
        }
    )
    with pytest.raises(ValueError, match="multiple publishable keys"):
        _catalog_key_by_token_name(catalog)


def test_pr170_finding_6_library_filter_disambiguates_duplicate_names() -> None:
    """Same setup; passing a single library_hash filter resolves the dup."""
    catalog = TokenCatalog.model_validate(
        {
            "schema_version": 2,
            "libraries": {
                "libold": {"name": "OLD", "source": "figma_api"},
                "libnew": {"name": "NEW", "source": "figma_api"},
            },
            "variables": {
                "VariableID:libold/1:1": {
                    "library_hash": "libold",
                    "collection_id": "c1",
                    "name": "fg/inverse",
                    "key": "old-key",
                    "resolved_type": "COLOR",
                    "values_by_mode": {"light": {"hex": "#000"}},
                    "source": "figma_api",
                },
                "VariableID:libnew/1:1": {
                    "library_hash": "libnew",
                    "collection_id": "c1",
                    "name": "fg/inverse",
                    "key": "new-key",
                    "resolved_type": "COLOR",
                    "values_by_mode": {"light": {"hex": "#FFF"}},
                    "source": "figma_api",
                },
            },
        }
    )
    result = _catalog_key_by_token_name(catalog, library_hashes={"libnew"})
    assert result == {"fg/inverse": "new-key"}


# Finding #4 — ok expression has single source of truth --------------------


def test_pr170_finding_4_summary_ok_uses_only_hardfailures() -> None:
    """`ok` derives from `hardFailures === 0` only — `aborted_by_signature`
    is already in the sum so adding `!signatureAbort` would be a
    redundant safety net likely to drift on refactor.
    """
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    assert "ok: hardFailures === 0," in js_no_comments
    # The redundant form must NOT be present.
    assert "ok: hardFailures === 0 && !signatureAbort" not in js_no_comments


# OperatorAction pydantic model -------------------------------------------


def test_pr170_finding_9_operator_action_is_pydantic_model() -> None:
    """OperatorAction has a stable shape consumers can depend on."""
    action = OperatorAction(
        signature="unloadable_font:Boldonse Bold",
        count=12,
        sample_rows=["1:1", "1:2"],
        instruction="upload it",
    )
    payload = action.model_dump(mode="json")
    assert payload["signature"] == "unloadable_font:Boldonse Bold"
    assert payload["count"] == 12
    assert payload["sample_rows"] == ["1:1", "1:2"]
    assert payload["instruction"] == "upload it"
    assert payload["additional_signatures"] == []
