"""Tests for per-frame content hashing.

INVARIANTS:
- compute_frame_hash detects child add/remove/rename
- compute_frame_hash detects text content changes
- compute_frame_hash detects component swaps
- compute_frame_hash ignores position/size changes (no false positives)
- compute_frame_hashes returns hashes for all frames in a page
"""

from __future__ import annotations

from figmaclaw.figma_hash import compute_frame_hash, compute_frame_hashes


def _frame(name: str = "login", children: list[dict] | None = None) -> dict:
    return {"id": "11:1", "name": name, "type": "FRAME", "children": children or []}


def test_frame_hash_returns_8_char_hex():
    h = compute_frame_hash(_frame())
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


def test_frame_hash_stable():
    a = compute_frame_hash(_frame("login", [{"name": "btn", "type": "INSTANCE"}]))
    b = compute_frame_hash(_frame("login", [{"name": "btn", "type": "INSTANCE"}]))
    assert a == b


def test_frame_hash_detects_child_added():
    before = compute_frame_hash(_frame("login", [
        {"name": "email", "type": "INSTANCE"},
    ]))
    after = compute_frame_hash(_frame("login", [
        {"name": "email", "type": "INSTANCE"},
        {"name": "forgot-password", "type": "TEXT", "characters": "Forgot?"},
    ]))
    assert before != after


def test_frame_hash_detects_child_removed():
    before = compute_frame_hash(_frame("login", [
        {"name": "email", "type": "INSTANCE"},
        {"name": "password", "type": "INSTANCE"},
    ]))
    after = compute_frame_hash(_frame("login", [
        {"name": "email", "type": "INSTANCE"},
    ]))
    assert before != after


def test_frame_hash_detects_child_renamed():
    before = compute_frame_hash(_frame("login", [
        {"name": "sign-in-btn", "type": "INSTANCE"},
    ]))
    after = compute_frame_hash(_frame("login", [
        {"name": "log-in-btn", "type": "INSTANCE"},
    ]))
    assert before != after


def test_frame_hash_detects_text_change():
    before = compute_frame_hash(_frame("login", [
        {"name": "cta", "type": "TEXT", "characters": "Sign In"},
    ]))
    after = compute_frame_hash(_frame("login", [
        {"name": "cta", "type": "TEXT", "characters": "Log In"},
    ]))
    assert before != after


def test_frame_hash_detects_component_swap():
    before = compute_frame_hash(_frame("login", [
        {"name": "btn", "type": "INSTANCE", "componentId": "comp:1"},
    ]))
    after = compute_frame_hash(_frame("login", [
        {"name": "btn", "type": "INSTANCE", "componentId": "comp:2"},
    ]))
    assert before != after


def test_frame_hash_detects_frame_rename():
    before = compute_frame_hash(_frame("login - default"))
    after = compute_frame_hash(_frame("login - error"))
    assert before != after


def test_frame_hash_ignores_position():
    """Position changes don't make descriptions stale."""
    before = compute_frame_hash({"id": "1:1", "name": "login", "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 100, "height": 200},
        "children": [{"name": "btn", "type": "INSTANCE"}]})
    after = compute_frame_hash({"id": "1:1", "name": "login", "type": "FRAME",
        "absoluteBoundingBox": {"x": 500, "y": 300, "width": 100, "height": 200},
        "children": [{"name": "btn", "type": "INSTANCE"}]})
    assert before == after


def test_frame_hash_ignores_child_order():
    """Child order doesn't matter — sorted for stability."""
    a = compute_frame_hash(_frame("login", [
        {"name": "email", "type": "INSTANCE"},
        {"name": "password", "type": "INSTANCE"},
    ]))
    b = compute_frame_hash(_frame("login", [
        {"name": "password", "type": "INSTANCE"},
        {"name": "email", "type": "INSTANCE"},
    ]))
    assert a == b


def test_compute_frame_hashes_returns_all_frames():
    page_node = {
        "id": "0:1",
        "name": "Page",
        "type": "CANVAS",
        "children": [
            {
                "id": "10:1",
                "name": "section",
                "type": "SECTION",
                "children": [
                    {"id": "11:1", "name": "frame-a", "type": "FRAME", "children": []},
                    {"id": "11:2", "name": "frame-b", "type": "FRAME", "children": []},
                ],
            },
            {"id": "12:1", "name": "ungrouped-frame", "type": "FRAME", "children": []},
        ],
    }
    hashes = compute_frame_hashes(page_node)
    assert "11:1" in hashes
    assert "11:2" in hashes
    assert "12:1" in hashes
    assert len(hashes) == 3


def test_compute_frame_hashes_empty_page():
    page_node = {"id": "0:1", "name": "Empty", "type": "CANVAS", "children": []}
    assert compute_frame_hashes(page_node) == {}
