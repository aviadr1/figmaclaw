"""Tests for save_catalog / merge_bindings idempotency.

INVARIANTS (canon §4 W-1, TC-8):
- save_catalog does NOT modify the file when load-bearing content is unchanged
  (only updated_at / fetched_at would differ — suppressed to avoid spurious
  git commits on every pull)
- save_catalog DOES write when load-bearing content changes
- merge_bindings + save_catalog produces a file write when usage signals
  change (canon TC-1, D13: page-walk merges accumulate usage_count, so
  the same bindings observed twice IS a content change)
"""

from __future__ import annotations

import json
from pathlib import Path

from figmaclaw.token_catalog import TokenCatalog, merge_bindings, save_catalog
from figmaclaw.token_scan import ValidBinding


def _make_binding(
    variable_id: str = "var:1", prop: str = "fill", hex: str = "#FF0000"
) -> ValidBinding:
    return ValidBinding(variable_id=variable_id, property=prop, hex=hex)


def test_save_catalog_creates_file_on_first_call(tmp_path: Path):
    """INVARIANT: save_catalog writes the catalog file on first call."""
    catalog = TokenCatalog()
    merge_bindings(catalog, [_make_binding()])

    save_catalog(catalog, tmp_path)

    path = tmp_path / ".figma-sync" / "ds_catalog.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert "var:1" in data["variables"]


def test_save_catalog_is_idempotent_when_state_unchanged(tmp_path: Path):
    """INVARIANT (W-1, TC-8): repeated save_catalog calls on an UNCHANGED catalog
    state must not modify the file. Only updated_at / fetched_at would differ,
    and write_json_if_changed strips those before comparison."""
    catalog = TokenCatalog()
    merge_bindings(catalog, [_make_binding()])
    save_catalog(catalog, tmp_path)

    path = tmp_path / ".figma-sync" / "ds_catalog.json"
    content_first = path.read_text()
    mtime_first = path.stat().st_mtime_ns

    # Same catalog state — refresh updated_at via touching it then save again.
    catalog.updated_at = "2099-01-01T00:00:00Z"
    save_catalog(catalog, tmp_path)

    content_second = path.read_text()
    mtime_second = path.stat().st_mtime_ns

    assert content_first == content_second
    assert mtime_first == mtime_second


def test_merge_bindings_increments_usage_count(tmp_path: Path):
    """INVARIANT (TC-1, D13): merge_bindings is the canonical writer for
    usage_count. Observing the same binding twice is meaningful state — it
    means the variable is used twice — and DOES produce a file write."""
    catalog = TokenCatalog()
    merge_bindings(catalog, [_make_binding()])
    save_catalog(catalog, tmp_path)

    path = tmp_path / ".figma-sync" / "ds_catalog.json"
    content_first = path.read_text()
    data_first = json.loads(content_first)
    assert data_first["variables"]["var:1"]["usage_count"] == 1

    merge_bindings(catalog, [_make_binding()])
    save_catalog(catalog, tmp_path)

    data_second = json.loads(path.read_text())
    assert data_second["variables"]["var:1"]["usage_count"] == 2


def test_save_catalog_writes_when_new_variable_added(tmp_path: Path):
    """INVARIANT: save_catalog writes when a new variable ID appears."""
    catalog = TokenCatalog()
    merge_bindings(catalog, [_make_binding("var:1")])
    save_catalog(catalog, tmp_path)

    path = tmp_path / ".figma-sync" / "ds_catalog.json"
    content_before = path.read_text()

    merge_bindings(catalog, [_make_binding("var:2", hex="#00FF00")])
    save_catalog(catalog, tmp_path)

    content_after = path.read_text()
    assert content_before != content_after
    data = json.loads(content_after)
    assert "var:1" in data["variables"]
    assert "var:2" in data["variables"]


def test_save_catalog_writes_when_observed_property_added(tmp_path: Path):
    """INVARIANT: save_catalog writes when an existing variable gains a new observed property."""
    catalog = TokenCatalog()
    merge_bindings(catalog, [_make_binding("var:1", prop="fill")])
    save_catalog(catalog, tmp_path)

    path = tmp_path / ".figma-sync" / "ds_catalog.json"
    content_before = path.read_text()

    merge_bindings(catalog, [_make_binding("var:1", prop="stroke")])
    save_catalog(catalog, tmp_path)

    content_after = path.read_text()
    assert content_before != content_after
    data = json.loads(content_after)
    assert "stroke" in data["variables"]["var:1"]["observed_on"]
