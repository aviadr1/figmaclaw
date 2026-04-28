"""Tests for compact token sidecar format (schema v2).

INVARIANTS:
- _aggregate_issues groups identical (property, classification, value) into one entry with count
- _aggregate_issues preserves all fields needed by suggest-tokens (property, hex, current_value, classification)
- _aggregate_issues drops per-node fields (node_id, node_name, node_path, node_type, index)
- Different values for the same property produce separate entries
- Stale issues preserve stale_variable_id in aggregated output
- suggest_for_sidecar works correctly with schema v2 aggregated sidecars
- suggest_tokens_cmd stats correctly multiply by count
"""

from __future__ import annotations

import json
from pathlib import Path

from figmaclaw.pull_logic import _aggregate_issues, _write_token_sidecar
from figmaclaw.token_catalog import (
    CatalogValue,
    CatalogVariable,
    TokenCatalog,
    merge_bindings,
    suggest_for_sidecar,
)
from figmaclaw.token_scan import (
    Classification,
    FrameTokenScan,
    PageTokenScan,
    TokenIssue,
    ValidBinding,
)

RED = {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0}
BLUE = {"r": 0.0, "g": 0.0, "b": 1.0, "a": 1.0}


def _seed_variable(
    catalog: TokenCatalog,
    variable_id: str,
    *,
    prop: str,
    hex: str | None = None,
    numeric_value: float | None = None,
) -> None:
    catalog.variables[variable_id] = CatalogVariable(
        name=variable_id,
        values_by_mode={
            "_default": CatalogValue(hex=hex, numeric_value=numeric_value),
        },
        source="figma_api",
        observed_on=[prop],
    )


def _issue(
    node_id: str = "1:1",
    node_name: str = "rect",
    prop: str = "fill",
    classification: Classification = "raw",
    current_value: object = None,
    hex: str | None = None,
    stale_variable_id: str | None = None,
) -> TokenIssue:
    return TokenIssue(
        node_id=node_id,
        node_name=node_name,
        node_type="RECTANGLE",
        node_path=["frame", node_name],
        property=prop,
        classification=classification,
        current_value=current_value if current_value is not None else RED,
        hex=hex if hex is not None else "#FF0000",
        stale_variable_id=stale_variable_id,
    )


# _aggregate_issues: grouping


def test_aggregate_identical_issues_grouped_with_count():
    """INVARIANT: identical (property, classification, value) issues collapse into one entry with count."""
    issues = [
        _issue(node_id="1:1", node_name="bg1"),
        _issue(node_id="2:1", node_name="bg2"),
        _issue(node_id="3:1", node_name="bg3"),
    ]
    result = _aggregate_issues(issues)
    assert len(result) == 1
    assert result[0]["count"] == 3
    assert result[0]["property"] == "fill"
    assert result[0]["hex"] == "#FF0000"
    assert result[0]["classification"] == "raw"


def test_aggregate_different_values_produce_separate_entries():
    """INVARIANT: different hex values for the same property are separate entries."""
    issues = [
        _issue(node_id="1:1", current_value=RED, hex="#FF0000"),
        _issue(node_id="2:1", current_value=RED, hex="#FF0000"),
        _issue(node_id="3:1", current_value=BLUE, hex="#0000FF"),
    ]
    result = _aggregate_issues(issues)
    assert len(result) == 2
    counts = {e["hex"]: e["count"] for e in result}
    assert counts["#FF0000"] == 2
    assert counts["#0000FF"] == 1


def test_aggregate_different_properties_produce_separate_entries():
    """INVARIANT: different properties are never merged even with same value."""
    issues = [
        _issue(prop="cornerRadius", current_value=8.0, hex=None),
        _issue(prop="itemSpacing", current_value=8.0, hex=None),
    ]
    result = _aggregate_issues(issues)
    assert len(result) == 2
    props = {e["property"] for e in result}
    assert props == {"cornerRadius", "itemSpacing"}


def test_aggregate_different_classifications_produce_separate_entries():
    """INVARIANT: raw and stale with same value are separate entries."""
    stale_var = "VariableID:legacyabc/1:1"
    issues = [
        _issue(classification="raw"),
        _issue(classification="stale", stale_variable_id=stale_var),
    ]
    result = _aggregate_issues(issues)
    assert len(result) == 2
    classes = {e["classification"] for e in result}
    assert classes == {"raw", "stale"}


def test_aggregate_preserves_stale_variable_id():
    """INVARIANT: stale_variable_id is preserved in aggregated output."""
    stale_var = "VariableID:legacyabc/1:1"
    issues = [
        _issue(classification="stale", stale_variable_id=stale_var),
    ]
    result = _aggregate_issues(issues)
    assert result[0]["stale_variable_id"] == stale_var


def test_aggregate_drops_node_fields():
    """INVARIANT: per-node fields are not present in aggregated output."""
    issues = [_issue(node_id="1:1", node_name="bg")]
    result = _aggregate_issues(issues)
    entry = result[0]
    assert "node_id" not in entry
    assert "node_name" not in entry
    assert "node_type" not in entry
    assert "node_path" not in entry
    assert "index" not in entry


def test_aggregate_numeric_values_grouped():
    """INVARIANT: identical numeric values for the same property are grouped."""
    issues = [
        _issue(node_id="1:1", prop="cornerRadius", current_value=8.0, hex=None),
        _issue(node_id="2:1", prop="cornerRadius", current_value=8.0, hex=None),
        _issue(node_id="3:1", prop="cornerRadius", current_value=16.0, hex=None),
    ]
    result = _aggregate_issues(issues)
    assert len(result) == 2
    by_val = {e["current_value"]: e["count"] for e in result}
    assert by_val[8.0] == 2
    assert by_val[16.0] == 1


def test_aggregate_empty_issues():
    """INVARIANT: empty issue list produces empty result."""
    result = _aggregate_issues([])
    assert result == []


def test_aggregate_single_issue_has_count_one():
    """INVARIANT: a single issue produces one entry with count=1."""
    result = _aggregate_issues([_issue()])
    assert len(result) == 1
    assert result[0]["count"] == 1


def test_aggregate_preserves_current_value_for_colors():
    """INVARIANT: color current_value (dict) is preserved in aggregated output."""
    issues = [_issue(current_value=RED, hex="#FF0000")]
    result = _aggregate_issues(issues)
    assert result[0]["current_value"] == RED


# _aggregate_issues: invariants


def test_aggregate_sum_of_counts_equals_input_length():
    """INVARIANT: sum of all counts in aggregated output equals the number of input issues."""
    issues = (
        [_issue(node_id=f"{i}:1", current_value=RED, hex="#FF0000") for i in range(40)]
        + [_issue(node_id=f"{i}:2", current_value=BLUE, hex="#0000FF") for i in range(25)]
        + [
            _issue(node_id=f"{i}:3", prop="cornerRadius", current_value=8.0, hex=None)
            for i in range(15)
        ]
    )
    result = _aggregate_issues(issues)
    assert sum(e["count"] for e in result) == 80


def test_aggregate_is_deterministic_regardless_of_input_order():
    """INVARIANT: aggregation produces identical output regardless of input order."""
    import random

    issues_a = [
        _issue(node_id="1:1", current_value=RED, hex="#FF0000"),
        _issue(node_id="2:1", current_value=BLUE, hex="#0000FF"),
        _issue(node_id="3:1", current_value=RED, hex="#FF0000"),
        _issue(node_id="4:1", prop="cornerRadius", current_value=8.0, hex=None),
        _issue(node_id="5:1", current_value=BLUE, hex="#0000FF"),
        _issue(node_id="6:1", prop="cornerRadius", current_value=8.0, hex=None),
    ]

    result_a = _aggregate_issues(issues_a)

    # Shuffle and aggregate again — must produce same result
    issues_b = list(issues_a)
    random.seed(42)
    random.shuffle(issues_b)
    result_b = _aggregate_issues(issues_b)

    # Sort both by a stable key for comparison
    def sort_key(entry: dict) -> tuple:
        return (entry["property"], entry.get("hex", ""), str(entry.get("current_value", "")))

    assert sorted(result_a, key=sort_key) == sorted(result_b, key=sort_key)
    assert sum(e["count"] for e in result_a) == sum(e["count"] for e in result_b) == 6


def test_aggregate_each_entry_has_all_required_fields():
    """INVARIANT: every aggregated entry has property, classification, and count."""
    issues = [
        _issue(prop="fill", current_value=RED, hex="#FF0000"),
        _issue(prop="cornerRadius", current_value=8.0, hex=None),
        _issue(
            prop="stroke",
            classification="stale",
            current_value=BLUE,
            hex="#0000FF",
            stale_variable_id="VariableID:legacyabc/1:1",
        ),
    ]
    result = _aggregate_issues(issues)
    for entry in result:
        assert "property" in entry
        assert "classification" in entry
        assert "count" in entry
        assert isinstance(entry["count"], int)
        assert entry["count"] >= 1


# _write_token_sidecar: idempotency with aggregation


def test_sidecar_idempotent_with_aggregated_multi_issue_input(tmp_path: Path):
    """INVARIANT: repeated writes of the same multi-issue input produce byte-identical files.

    This is the critical idempotency property that prevents spurious git commits.
    With aggregation, the output must be stable across calls — same grouping,
    same order, same counts.
    """
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")

    issues = (
        [_issue(node_id=f"{i}:1", current_value=RED, hex="#FF0000") for i in range(50)]
        + [_issue(node_id=f"{i}:2", current_value=BLUE, hex="#0000FF") for i in range(30)]
        + [
            _issue(node_id=f"{i}:3", prop="cornerRadius", current_value=8.0, hex=None)
            for i in range(20)
        ]
    )
    fscan = FrameTokenScan(name="frame", raw=100, issues=issues)
    scan = PageTokenScan(raw=100, frames={"1:1": fscan})

    _write_token_sidecar(screen_md, "abc", "0:1", scan)
    sidecar = tmp_path / "page.tokens.json"
    content_first = sidecar.read_text()
    mtime_first = sidecar.stat().st_mtime_ns

    _write_token_sidecar(screen_md, "abc", "0:1", scan)
    content_second = sidecar.read_text()
    mtime_second = sidecar.stat().st_mtime_ns

    assert content_first == content_second
    assert mtime_first == mtime_second


def test_sidecar_not_idempotent_when_issues_change(tmp_path: Path):
    """INVARIANT: sidecar IS rewritten when issue data changes (change detection works)."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")

    issues_v1 = [_issue(node_id=f"{i}:1") for i in range(10)]
    scan_v1 = PageTokenScan(
        raw=10, frames={"1:1": FrameTokenScan(name="f", raw=10, issues=issues_v1)}
    )
    _write_token_sidecar(screen_md, "abc", "0:1", scan_v1)
    sidecar = tmp_path / "page.tokens.json"
    content_before = sidecar.read_text()

    # Add a new type of issue
    issues_v2 = issues_v1 + [
        _issue(node_id="99:1", prop="cornerRadius", current_value=16.0, hex=None)
    ]
    scan_v2 = PageTokenScan(
        raw=11, frames={"1:1": FrameTokenScan(name="f", raw=11, issues=issues_v2)}
    )
    _write_token_sidecar(screen_md, "abc", "0:1", scan_v2)
    content_after = sidecar.read_text()

    assert content_before != content_after
    data = json.loads(content_after)
    assert data["summary"]["raw"] == 11
    assert len(data["frames"]["1:1"]["issues"]) == 2


def test_sidecar_not_idempotent_when_counts_change(tmp_path: Path):
    """INVARIANT: sidecar IS rewritten when the same value appears more/fewer times."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")

    issues_5 = [_issue(node_id=f"{i}:1") for i in range(5)]
    scan_5 = PageTokenScan(raw=5, frames={"1:1": FrameTokenScan(name="f", raw=5, issues=issues_5)})
    _write_token_sidecar(screen_md, "abc", "0:1", scan_5)
    sidecar = tmp_path / "page.tokens.json"
    data_before = json.loads(sidecar.read_text())

    issues_10 = [_issue(node_id=f"{i}:1") for i in range(10)]
    scan_10 = PageTokenScan(
        raw=10, frames={"1:1": FrameTokenScan(name="f", raw=10, issues=issues_10)}
    )
    _write_token_sidecar(screen_md, "abc", "0:1", scan_10)
    data_after = json.loads(sidecar.read_text())

    assert data_before["frames"]["1:1"]["issues"][0]["count"] == 5
    assert data_after["frames"]["1:1"]["issues"][0]["count"] == 10


# _write_token_sidecar: schema v2 structure


def test_sidecar_has_schema_version_2(tmp_path: Path):
    """INVARIANT: written sidecar includes schema_version: 2."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")
    issue = _issue()
    fscan = FrameTokenScan(name="frame", raw=1, issues=[issue])
    scan = PageTokenScan(raw=1, frames={"1:1": fscan})

    _write_token_sidecar(screen_md, "abc", "0:1", scan)

    data = json.loads((tmp_path / "page.tokens.json").read_text())
    assert data["schema_version"] == 2


def test_sidecar_issues_are_aggregated(tmp_path: Path):
    """INVARIANT: multiple identical issues are aggregated with count in written sidecar."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")
    issues = [
        _issue(node_id="1:1", node_name="bg1"),
        _issue(node_id="2:1", node_name="bg2"),
        _issue(node_id="3:1", node_name="bg3"),
    ]
    fscan = FrameTokenScan(name="frame", raw=3, issues=issues)
    scan = PageTokenScan(raw=3, frames={"1:1": fscan})

    _write_token_sidecar(screen_md, "abc", "0:1", scan)

    data = json.loads((tmp_path / "page.tokens.json").read_text())
    frame_issues = data["frames"]["1:1"]["issues"]
    assert len(frame_issues) == 1
    assert frame_issues[0]["count"] == 3
    assert frame_issues[0]["property"] == "fill"


def test_sidecar_summary_reflects_total_issues_not_unique(tmp_path: Path):
    """INVARIANT: summary counts reflect total issues (not unique combos)."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")
    issues = [
        _issue(node_id="1:1"),
        _issue(node_id="2:1"),
    ]
    fscan = FrameTokenScan(name="frame", raw=2, issues=issues)
    scan = PageTokenScan(raw=2, frames={"1:1": fscan})

    _write_token_sidecar(screen_md, "abc", "0:1", scan)

    data = json.loads((tmp_path / "page.tokens.json").read_text())
    assert data["summary"]["raw"] == 2
    assert data["frames"]["1:1"]["summary"]["raw"] == 2


# suggest_for_sidecar: compatibility with schema v2


def test_suggest_for_sidecar_works_with_aggregated_issues():
    """INVARIANT: suggest_for_sidecar correctly enriches schema v2 aggregated issues."""
    catalog = TokenCatalog()
    _seed_variable(catalog, "var:red", prop="fill", hex="#FF0000")
    merge_bindings(
        catalog,
        [
            ValidBinding(variable_id="var:red", property="fill", hex="#FF0000"),
        ],
    )

    sidecar = {
        "schema_version": 2,
        "frames": {
            "1:1": {
                "name": "frame",
                "summary": {"raw": 5, "stale": 0, "valid": 0},
                "issues": [
                    {"property": "fill", "hex": "#FF0000", "classification": "raw", "count": 5},
                ],
            }
        },
    }

    suggest_for_sidecar(sidecar, catalog)

    issue = sidecar["frames"]["1:1"]["issues"][0]
    assert issue["suggest_status"] == "auto"
    assert issue["fix_variable_id"] == "var:red"
    assert issue["candidates"] == ["var:red"]
    assert issue["count"] == 5


def test_suggest_for_sidecar_numeric_with_aggregated_issues():
    """INVARIANT: numeric matching works correctly with aggregated issues."""
    catalog = TokenCatalog()
    _seed_variable(catalog, "var:radius-8", prop="cornerRadius", numeric_value=8.0)
    merge_bindings(
        catalog,
        [
            ValidBinding(variable_id="var:radius-8", property="cornerRadius", numeric_value=8.0),
        ],
    )

    sidecar = {
        "schema_version": 2,
        "frames": {
            "1:1": {
                "name": "frame",
                "summary": {"raw": 10, "stale": 0, "valid": 0},
                "issues": [
                    {
                        "property": "cornerRadius",
                        "current_value": 8.0,
                        "classification": "raw",
                        "count": 7,
                    },
                    {
                        "property": "cornerRadius",
                        "current_value": 16.0,
                        "classification": "raw",
                        "count": 3,
                    },
                ],
            }
        },
    }

    suggest_for_sidecar(sidecar, catalog)

    issues = sidecar["frames"]["1:1"]["issues"]
    matched = [i for i in issues if i["suggest_status"] == "auto"]
    unmatched = [i for i in issues if i["suggest_status"] == "no_match"]
    assert len(matched) == 1
    assert matched[0]["count"] == 7
    assert matched[0]["fix_variable_id"] == "var:radius-8"
    assert len(unmatched) == 1
    assert unmatched[0]["count"] == 3


# Smoke test: end-to-end pull → suggest roundtrip


def test_end_to_end_pull_suggest_roundtrip(tmp_path: Path):
    """SMOKE: pull writes compact sidecar, suggest-tokens enriches it correctly."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")

    # Simulate pull: many nodes with same red fill + a few blue fills
    issues = (
        [
            _issue(node_id=f"{i}:1", node_name=f"rect{i}", current_value=RED, hex="#FF0000")
            for i in range(100)
        ]
        + [
            _issue(node_id=f"{i}:2", node_name=f"blue{i}", current_value=BLUE, hex="#0000FF")
            for i in range(20)
        ]
        + [
            _issue(
                node_id=f"{i}:3",
                node_name=f"radius{i}",
                prop="cornerRadius",
                current_value=8.0,
                hex=None,
            )
            for i in range(50)
        ]
    )
    fscan = FrameTokenScan(name="main", raw=170, issues=issues)
    scan = PageTokenScan(raw=170, frames={"1:1": fscan})

    _write_token_sidecar(screen_md, "abc", "0:1", scan)

    sidecar_path = tmp_path / "page.tokens.json"
    data = json.loads(sidecar_path.read_text())

    # Verify compactness: 170 issues → 3 unique entries
    frame_issues = data["frames"]["1:1"]["issues"]
    assert len(frame_issues) == 3
    total_count = sum(e["count"] for e in frame_issues)
    assert total_count == 170

    # Now run suggest-tokens
    catalog = TokenCatalog()
    _seed_variable(catalog, "var:red", prop="fill", hex="#FF0000")
    _seed_variable(catalog, "var:radius-8", prop="cornerRadius", numeric_value=8.0)
    merge_bindings(
        catalog,
        [
            ValidBinding(variable_id="var:red", property="fill", hex="#FF0000"),
            ValidBinding(variable_id="var:radius-8", property="cornerRadius", numeric_value=8.0),
        ],
    )

    suggest_for_sidecar(data, catalog)

    # Verify suggestions
    auto_issues = [i for i in data["frames"]["1:1"]["issues"] if i.get("suggest_status") == "auto"]
    no_match_issues = [
        i for i in data["frames"]["1:1"]["issues"] if i.get("suggest_status") == "no_match"
    ]

    assert len(auto_issues) == 2  # red fill + radius-8
    assert sum(i["count"] for i in auto_issues) == 150  # 100 + 50
    assert len(no_match_issues) == 1  # blue fill
    assert no_match_issues[0]["count"] == 20


def test_sidecar_file_size_dramatically_smaller(tmp_path: Path):
    """SMOKE: a sidecar with 10,000 identical issues is tiny, not megabytes."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")

    issues = [_issue(node_id=f"{i}:1", node_name=f"node{i}") for i in range(10000)]
    fscan = FrameTokenScan(name="huge-frame", raw=10000, issues=issues)
    scan = PageTokenScan(raw=10000, frames={"1:1": fscan})

    _write_token_sidecar(screen_md, "abc", "0:1", scan)

    sidecar_path = tmp_path / "page.tokens.json"
    size_bytes = sidecar_path.stat().st_size
    data = json.loads(sidecar_path.read_text())

    # 10,000 issues → 1 unique entry
    assert len(data["frames"]["1:1"]["issues"]) == 1
    assert data["frames"]["1:1"]["issues"][0]["count"] == 10000
    # File should be well under 1 KB (not the megabytes of v1)
    assert size_bytes < 1024


def test_multi_frame_aggregation(tmp_path: Path):
    """SMOKE: aggregation works independently per frame."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc\n---\n")

    frame_a_issues = [
        _issue(node_id=f"a{i}:1", current_value=RED, hex="#FF0000") for i in range(50)
    ]
    frame_b_issues = [
        _issue(node_id=f"b{i}:1", current_value=BLUE, hex="#0000FF") for i in range(30)
    ]

    scan = PageTokenScan(
        raw=80,
        frames={
            "1:1": FrameTokenScan(name="frame-a", raw=50, issues=frame_a_issues),
            "2:1": FrameTokenScan(name="frame-b", raw=30, issues=frame_b_issues),
        },
    )

    _write_token_sidecar(screen_md, "abc", "0:1", scan)

    data = json.loads((tmp_path / "page.tokens.json").read_text())
    assert len(data["frames"]["1:1"]["issues"]) == 1
    assert data["frames"]["1:1"]["issues"][0]["count"] == 50
    assert data["frames"]["1:1"]["issues"][0]["hex"] == "#FF0000"
    assert len(data["frames"]["2:1"]["issues"]) == 1
    assert data["frames"]["2:1"]["issues"][0]["count"] == 30
    assert data["frames"]["2:1"]["issues"][0]["hex"] == "#0000FF"
