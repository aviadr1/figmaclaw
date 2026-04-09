"""Tests for save_catalog / merge_bindings idempotency.

INVARIANTS:
- save_catalog does NOT modify the file when variable data is unchanged
  (only updated_at would differ — suppressed to avoid spurious git commits)
- save_catalog DOES write when variable data changes
- merge_bindings + save_catalog is idempotent when called repeatedly with
  the same bindings (same pattern as _write_token_sidecar)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from figmaclaw.token_catalog import TokenCatalog, merge_bindings, save_catalog
from figmaclaw.token_scan import ValidBinding


def _make_binding(variable_id: str = "var:1", prop: str = "fill", hex: str = "#FF0000") -> ValidBinding:
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


def test_save_catalog_is_idempotent_when_data_unchanged(tmp_path: Path):
    """INVARIANT: repeated save_catalog calls with identical variable data must not modify the file.

    Before the fix, save_catalog always wrote updated_at = now, causing a spurious
    git commit on every pull even when no new DS bindings were discovered.
    After the fix, only variable data changes cause a write.
    """
    catalog = TokenCatalog()
    merge_bindings(catalog, [_make_binding()])
    save_catalog(catalog, tmp_path)

    path = tmp_path / ".figma-sync" / "ds_catalog.json"
    content_first = path.read_text()
    mtime_first = path.stat().st_mtime_ns

    # Simulate a second pull: same bindings, merge again
    merge_bindings(catalog, [_make_binding()])
    save_catalog(catalog, tmp_path)

    content_second = path.read_text()
    mtime_second = path.stat().st_mtime_ns

    assert content_first == content_second
    assert mtime_first == mtime_second


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
