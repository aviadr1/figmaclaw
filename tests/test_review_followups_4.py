"""Tests for the fourth-round PR #167 review fixes (Copilot on commit e9408cf)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from click.testing import CliRunner

from figmaclaw.audit import VALIDATION_BOOLS
from figmaclaw.audit_page_swap import SwapRow, render_swap_script
from figmaclaw.component_map import NestedRuleValidation
from figmaclaw.main import cli

# Copilot 3211429163 — VALIDATION_BOOLS derived from pydantic ------------


def test_round4_validation_bools_derived_from_pydantic_model() -> None:
    """Adding a new boolean to NestedRuleValidation must update VALIDATION_BOOLS."""
    assert set(NestedRuleValidation.model_fields) == VALIDATION_BOOLS
    # Sanity check the canonical four are still present so a future
    # accidental rename of a model field surfaces here too.
    assert {
        "assert_target_type",
        "assert_name_matches",
        "assert_property_keys",
        "assert_variant_axes",
    } <= VALIDATION_BOOLS


# Copilot 3211429187 — figma.mixed font handling -------------------------


def test_round4_swap_js_skips_mixed_font_in_load_chain() -> None:
    """Rendered JS picks the first non-mixed font candidate, not the first truthy one.

    A target text node with `fontName === figma.mixed` (truthy) used to
    short-circuit the `target.fontName || o.fontName` selector, skip
    loadFontAsync, and then crash `target.characters = …` with
    "font not loaded".
    """
    js = render_swap_script(
        page_node_id="9559:29",
        namespace="ns",
        rows=[SwapRow(src="a", newKey="b")],
    )
    # The new logic builds a candidates array and finds() the first usable.
    assert "candidates.find(" in js
    assert "s !== figma.mixed" in js
    # The old short-circuit form must NOT be present.
    js_no_comments = "\n".join(re.sub(r"//.*", "", line) for line in js.splitlines())
    assert "target.fontName || o.fontName" not in js_no_comments


# Copilot 3211429212 — --batch-size click.IntRange validation -----------


def test_round4_batch_size_zero_rejected_with_click_usage_error(tmp_path: Path) -> None:
    """`--batch-size 0` produces a Click UsageError, not a Python stack trace."""
    manifest = tmp_path / "swap.json"
    manifest.write_text(
        json.dumps([{"src": "1", "oldCid": "OLD", "newKey": "K"}]),
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
            "--batch-size",
            "0",
            "--emit-only",
            "--batch-dir",
            "swap_batches",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    # Click formats IntRange violations as a clear "Invalid value for ..."
    # error rather than a Python traceback.
    assert "Invalid value for '--batch-size'" in result.output
    assert "Traceback" not in result.output


def test_round4_batch_size_negative_rejected(tmp_path: Path) -> None:
    """Same protection for negative batch sizes via the apply-tokens command."""
    rows_path = tmp_path / "rows.json"
    rows_path.write_text("[]", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "apply-tokens",
            str(rows_path),
            "--file",
            "F",
            "--page",
            "P",
            "--batch-size",
            "-5",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "Invalid value for '--batch-size'" in result.output
