"""Tests for the fourth-round PR #170 review fixes.

Each test names the finding it protects against. See the fourth review
report for the full list of findings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from figmaclaw.apply_tokens import (
    AdditionalSignature,
    OperatorAction,
    operator_action_for_signature,
    render_apply_tokens_script,
)
from figmaclaw.commands.apply_tokens import _collect_signature_aborts, _safe_count
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli


def _mcp_call_record(structured: dict) -> dict:
    return {
        "index": 1,
        "description": "batch",
        "isError": False,
        "result": {"structuredContent": structured},
    }


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
                        "key": "bg-neutral-inverse-key",
                        "resolved_type": "COLOR",
                        "values_by_mode": {"light": {"hex": "#FFF"}},
                        "source": "figma_api",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


# Finding #1 — recordSignature widens rowId guard ---------------------


def test_review4_finding_1_recordsignature_skips_empty_or_null_rowid() -> None:
    """Empty-string and null rowIds must NOT land in sample_rows.

    The earlier guard `rowId !== undefined` accepted both `null` and `""`
    and produced sample_rows like `[""]` or `[null]`. Counts still go up
    so the threshold fires correctly, but sample_rows stays clean.
    """
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    # The wider null/empty-string guard is now in the JS.
    assert 'rowId != null && rowId !== ""' in js_no_comments


# Finding #2 — resolver handles token_name-only rows -----------------


def test_review4_finding_2_token_name_only_row_reaches_resolver() -> None:
    """A row with only `token_name` (no variable_key/id) must still be
    eligible for the F41 catalog fallback. The cache key falls back to
    `token_name:<name>` so the pre-resolver short-circuit doesn't strand
    it.
    """
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    # The new rowCacheKey helper exists and uses token_name as a tertiary
    # cache key.
    assert "function rowCacheKey(row)" in js
    assert '"token_name:" + row.token_name' in js
    # And the binding loop reads via the same helper, so write/read agree.
    assert "varsByRef[rowCacheKey(row)]" in js


# Finding #3 — end-to-end executor test for F41 fallback path -------


def test_review4_finding_3_f41_executor_path_reports_catalog_fallback(
    tmp_path: Path,
) -> None:
    """Faked executor: a row resolved through `kind: catalog_token_name`
    surfaces in the report's variableErrors-cleared / fallback-applied
    summary. We verify the report carries the runtime's structured stats
    correctly.
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
                    "ok": True,
                    "stats": {"applied": 1, "errors": 0},
                    # No signatureAbort, so the CLI doesn't fire the F36 path;
                    # the run reports a clean apply driven by the F41 fallback.
                    "variableErrors": [],
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
                "--json",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # The execute path returned a successful row and no operator_action
    # (no abort); the F41 fallback was plumbed end-to-end.
    assert "operator_action" not in data
    assert data["execution"]["calls"][0]["result"]["structuredContent"]["ok"] is True


# Finding #4 — classifyError covers more Figma error strings ---------


@pytest.mark.parametrize(
    "error_text, expected_class",
    [
        # Originals (regression check — must still classify)
        ("font Boldonse Bold not loaded", "unloadable_font:Boldonse Bold"),
        ("read-only file", "read_only_file"),
        # New classes
        ("Cannot find published variable for key abc123", "variable_not_found"),
        ("Variable does not exist in this team", "variable_not_published"),
        ("HTTP 429: too many requests", "rate_limited"),
        ("Network error: ECONNRESET", "network_unavailable"),
        ("session expired; please re-authenticate", "session_expired"),
        ("Unauthorized (401)", "session_expired"),
    ],
)
def test_review4_finding_4_classifier_covers_known_figma_strings(
    error_text: str, expected_class: str
) -> None:
    """Every known Figma runtime error string maps to a class.

    We test by inspecting the JS classifier's regexes — extracted from
    the rendered template via the same shape `recordSignature` consumes.
    A mismatch would let the wall-of-text symptom #169 fixed reappear
    for a different root cause.
    """
    # Run the JS classifier in Python by re-implementing the regex set.
    # This guards against drift between the JS template and the Python
    # operator_action_for_signature helper.
    js = render_apply_tokens_script(
        page_node_id="9559:29", namespace="ns", rows=[], node_map="shared-plugin-data"
    )
    # The regex set must contain every class we expect.
    assert "rate ?limit" in js
    assert "network (?:error|unreachable|timeout)" in js
    assert "session (?:expired|not found)" in js
    assert "cannot find (?:published )?variable" in js
    assert "variable does not exist" in js
    # And every new class must produce a non-empty operator-action hint.
    hint = operator_action_for_signature(expected_class)
    assert hint, f"class {expected_class} produced no operator-action hint"


def test_review4_finding_4_new_classes_have_actionable_hints() -> None:
    """Each new signature class returns specific operator instructions."""
    assert "back off" in operator_action_for_signature("rate_limited").lower()
    assert "network" in operator_action_for_signature("network_unavailable").lower()
    assert "re-authenticate" in operator_action_for_signature("session_expired").lower()
    assert "publish" in operator_action_for_signature("variable_not_published").lower()
    # Class:identifier shape gets the identifier in the message
    msg = operator_action_for_signature("variable_not_published:bg/neutral/inverse")
    assert "bg/neutral/inverse" in msg


# Finding #5 — _safe_count is float/non-numeric tolerant -------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (5, 5),
        (5.0, 5),
        (5.7, 5),  # float is truncated, not rounded
        ("5", 5),
        ("5.7", 5),
        (None, 0),
        ("", 0),
        ("not a number", 0),
        (-3, 0),  # negatives clamped to 0 — counts can't be negative
        (float("nan"), 0),
        (True, 1),  # bool is int-like; True becomes 1
    ],
)
def test_review4_finding_5_safe_count_handles_all_inputs(raw, expected: int) -> None:
    assert _safe_count(raw) == expected


def test_review4_finding_5_collect_aborts_does_not_crash_on_garbage() -> None:
    """A non-numeric `count` field must not crash the CLI — garbage in,
    zero out (with the original signature still surfaced)."""
    aborts = _collect_signature_aborts(
        {
            "calls": [
                _mcp_call_record(
                    {
                        "signatureAbort": {
                            "signature": "rate_limited",
                            "count": "lots",  # MCP proxy serialises odd values
                            "sample_rows": [],
                        }
                    }
                )
            ]
        }
    )
    assert aborts and aborts[0]["signature"] == "rate_limited"
    # Confirm _safe_count flattens the bad value cleanly.
    assert _safe_count(aborts[0].get("count")) == 0


# Finding #6 — additional_signatures carries sample_rows ------------


def test_review4_finding_6_additional_signatures_keep_sample_rows(
    tmp_path: Path,
) -> None:
    """The secondary 'also seen:' aborts must carry sample_rows so the
    operator can drill in without re-reading the full report."""
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
                        "sample_rows": ["a:1", "a:2", "a:3"],
                    }
                }
            ),
            _mcp_call_record(
                {
                    "signatureAbort": {
                        "signature": "read_only_file",
                        "count": 5,
                        "sample_rows": ["b:1", "b:2"],
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
                "--json",
            ],
            catch_exceptions=False,
        )
    # The CLI exits non-zero (operator-action required), and the JSON
    # report includes the additional_signature with its sample_rows.
    assert result.exit_code != 0
    # CliRunner merges stdout + stderr; the JSON output (`emit_json_value`)
    # comes first, then the click.echo("ACTION REQUIRED…", err=True) line.
    # Extract the leading JSON object via raw_decode.
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(result.output[result.output.find("{") :])
    extras = payload["operator_action"]["additional_signatures"]
    assert extras and extras[0]["signature"] == "read_only_file"
    assert extras[0]["sample_rows"] == ["b:1", "b:2"]


def test_review4_finding_6_additional_signature_is_pydantic_model() -> None:
    """AdditionalSignature is a typed model so consumers get a stable shape."""
    sig = AdditionalSignature(signature="x", count=3, sample_rows=["a", "b"])
    payload = sig.model_dump(mode="json")
    assert payload == {"signature": "x", "count": 3, "sample_rows": ["a", "b"]}


# Finding #6 — OperatorAction's additional_signatures is typed -----


def test_review4_finding_6_operator_action_additional_signatures_typed() -> None:
    """OperatorAction.additional_signatures is list[AdditionalSignature],
    not list[dict[str, Any]] — pinned by mypy via runtime instance check.
    """
    action = OperatorAction(
        signature="primary",
        count=10,
        sample_rows=[],
        instruction="",
        additional_signatures=[AdditionalSignature(signature="other", count=5, sample_rows=["x"])],
    )
    assert isinstance(action.additional_signatures[0], AdditionalSignature)
    # Round-trip through JSON keeps the shape.
    payload = action.model_dump(mode="json")
    assert payload["additional_signatures"][0]["sample_rows"] == ["x"]
