"""Tests for per-frame content hashing.

INVARIANTS:
- compute_frame_hash detects child add/remove/rename
- compute_frame_hash detects text content changes
- compute_frame_hash detects component swaps
- compute_frame_hash ignores position/size changes (no false positives)
- compute_frame_hash ignores invisible children
- compute_frame_hash detects visibility flips of children
- compute_frame_hashes returns hashes for all visible frames in a page
- compute_frame_hashes respects inherited visibility (hidden SECTION → children skipped)
"""

from __future__ import annotations

from figmaclaw.figma_hash import (
    compute_frame_hash,
    compute_frame_hashes,
    compute_page_hash,
)


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


# ---------------------------------------------------------------------------
# Visibility invariants — compute_frame_hash (depth-1 children)
# ---------------------------------------------------------------------------


def test_frame_hash_ignores_invisible_child():
    """Hidden TEXT layer inside a frame must NOT contribute to the hash.

    The stored description reflects the screenshot, which Figma renders
    without invisible children. If hidden content contributed, toggling
    visibility wouldn't mark the frame stale.
    """
    with_hidden = compute_frame_hash(_frame("login", [
        {"name": "title", "type": "TEXT", "characters": "Log in"},
        {"name": "debug-note", "type": "TEXT", "characters": "remove before launch",
         "visible": False},
    ]))
    without_it = compute_frame_hash(_frame("login", [
        {"name": "title", "type": "TEXT", "characters": "Log in"},
    ]))
    assert with_hidden == without_it, (
        "Hidden children must not affect the hash — they're invisible in "
        "the screenshot too, so they don't influence staleness"
    )


def test_frame_hash_detects_visibility_flip():
    """Un-hiding a child must change the frame hash (screenshot differs)."""
    before = compute_frame_hash(_frame("login", [
        {"name": "cta", "type": "TEXT", "characters": "Sign In", "visible": False},
    ]))
    after = compute_frame_hash(_frame("login", [
        {"name": "cta", "type": "TEXT", "characters": "Sign In", "visible": True},
    ]))
    assert before != after, "Flipping a child's visibility must change the hash"


def test_frame_hash_ignores_renames_of_invisible_children():
    """Renaming an already-invisible child must NOT change the hash."""
    before = compute_frame_hash(_frame("login", [
        {"name": "debug-a", "type": "TEXT", "characters": "x", "visible": False},
    ]))
    after = compute_frame_hash(_frame("login", [
        {"name": "debug-b", "type": "TEXT", "characters": "x", "visible": False},
    ]))
    assert before == after, (
        "Invisible children shouldn't contribute to the hash — renaming one "
        "has no visible effect and must not waste a re-description cycle"
    )


# ---------------------------------------------------------------------------
# Visibility invariants — compute_frame_hashes (batch, inherited visibility)
# ---------------------------------------------------------------------------


def test_compute_frame_hashes_skips_hidden_top_level_frame():
    page_node = {
        "id": "0:1", "name": "Page", "type": "CANVAS",
        "children": [
            {"id": "11:1", "name": "visible", "type": "FRAME", "children": []},
            {"id": "11:2", "name": "hidden", "type": "FRAME", "children": [],
             "visible": False},
        ],
    }
    hashes = compute_frame_hashes(page_node)
    assert "11:1" in hashes
    assert "11:2" not in hashes


def test_compute_frame_hashes_skips_hidden_section_children():
    """Inherited visibility: a hidden SECTION's visible children must NOT
    appear in frame_hashes. The whole subtree is invisible in the canvas."""
    page_node = {
        "id": "0:1", "name": "Page", "type": "CANVAS",
        "children": [
            {
                "id": "10:1", "name": "hidden section", "type": "SECTION",
                "visible": False,
                "children": [
                    {"id": "11:1", "name": "inner", "type": "FRAME", "children": []},
                    {"id": "11:2", "name": "inner-2", "type": "FRAME", "children": []},
                ],
            },
            {
                "id": "20:1", "name": "visible section", "type": "SECTION",
                "children": [
                    {"id": "21:1", "name": "still shown", "type": "FRAME", "children": []},
                ],
            },
        ],
    }
    hashes = compute_frame_hashes(page_node)
    assert "21:1" in hashes
    assert "11:1" not in hashes, "Hidden SECTION should hide its children"
    assert "11:2" not in hashes, "Hidden SECTION should hide its children"


def test_compute_frame_hashes_skips_hidden_grandchild_of_visible_section():
    page_node = {
        "id": "0:1", "name": "Page", "type": "CANVAS",
        "children": [
            {
                "id": "10:1", "name": "section", "type": "SECTION",
                "children": [
                    {"id": "11:1", "name": "visible", "type": "FRAME", "children": []},
                    {"id": "11:2", "name": "hidden", "type": "FRAME", "children": [],
                     "visible": False},
                ],
            },
        ],
    }
    hashes = compute_frame_hashes(page_node)
    assert "11:1" in hashes
    assert "11:2" not in hashes


# ---------------------------------------------------------------------------
# Visibility invariants — compute_page_hash
# ---------------------------------------------------------------------------


def _simple_page_with_frame(*, visible: bool = True, name: str = "login") -> dict:
    return {
        "id": "0:1", "name": "Page", "type": "CANVAS",
        "children": [
            {"id": "11:1", "name": name, "type": "FRAME", "children": [],
             "visible": visible},
        ],
    }


def test_page_hash_hiding_a_frame_changes_the_hash():
    """The core figmaclaw hash inconsistency: a visibility flip must
    produce a different hash so the page gets re-rendered and the
    markdown drops the hidden frame."""
    visible = compute_page_hash(_simple_page_with_frame(visible=True))
    hidden = compute_page_hash(_simple_page_with_frame(visible=False))
    assert visible != hidden, (
        "Hiding a frame must change the page hash so the re-render drops "
        "it from the rendered markdown"
    )


def test_page_hash_renaming_hidden_frame_is_a_no_op():
    """An invisible frame isn't rendered, so renaming it must not
    trigger a re-enrichment cycle."""
    a = compute_page_hash(_simple_page_with_frame(visible=False, name="debug-a"))
    b = compute_page_hash(_simple_page_with_frame(visible=False, name="debug-b"))
    assert a == b, (
        "Invisible frames don't appear in rendered markdown, so their names "
        "must not contribute to the hash"
    )


def test_page_hash_hidden_section_excludes_children_inherited_visibility():
    """Inherited visibility: renaming a frame inside a hidden SECTION must
    not change the hash, because that frame isn't rendered."""
    def _page(frame_name: str) -> dict:
        return {
            "id": "0:1", "name": "Page", "type": "CANVAS",
            "children": [
                {
                    "id": "10:1", "name": "hidden section", "type": "SECTION",
                    "visible": False,
                    "children": [
                        {"id": "11:1", "name": frame_name, "type": "FRAME",
                         "children": []},
                    ],
                },
            ],
        }
    assert compute_page_hash(_page("before")) == compute_page_hash(_page("after"))


def test_page_hash_visibility_round_trip_is_stable():
    """Toggling visibility off and on returns to the original hash."""
    original = compute_page_hash(_simple_page_with_frame(visible=True))
    hidden = compute_page_hash(_simple_page_with_frame(visible=False))
    restored = compute_page_hash(_simple_page_with_frame(visible=True))
    assert original == restored
    assert original != hidden


def test_page_hash_hiding_a_section_changes_the_hash():
    def _page(section_visible: bool) -> dict:
        return {
            "id": "0:1", "name": "Page", "type": "CANVAS",
            "children": [
                {
                    "id": "10:1", "name": "section", "type": "SECTION",
                    "visible": section_visible,
                    "children": [
                        {"id": "11:1", "name": "f", "type": "FRAME", "children": []},
                    ],
                },
            ],
        }
    visible = compute_page_hash(_page(True))
    hidden = compute_page_hash(_page(False))
    assert visible != hidden
