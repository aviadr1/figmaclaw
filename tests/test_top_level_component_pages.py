"""Regression tests for pages with top-level COMPONENT/COMPONENT_SET children.

# Hypothesis (Agent A, see docs/pr-129-investigation-agent-A.md H2):
#
# Several Gigaverse design-system pages are silently dropped by figmaclaw
# during pull because their COMPONENT_SETs sit directly on the page canvas
# (no SECTION wrapper, no FRAME wrapper). Concretely:
#
#   ✅ Tooltip & Help icon  → top-level COMPONENT_SETs (Tooltip, Help icon)
#   ☼ Logo                 → top-level COMPONENT_SETs (logo, logotype)
#   ☼ App Icon             → top-level COMPONENT_SETs
#   ☼ Date & Time Format   → top-level COMPONENT_SETs (per census)
#   ✅ Tooltip & Help icon → top-level COMPONENT_SETs
#
# The current ``from_page_node`` only handles top-level SECTION (with FRAME
# or COMPONENT children) and top-level FRAME (synthetic Ungrouped section).
# Top-level COMPONENT and COMPONENT_SET nodes fall through to no handler,
# producing ``sections=[]``. The page hash on those pages is the canonical
# ``sha256("[]")[:16] = "4f53cda18c2baa0c"`` — the same value across every
# affected page, which is why this bug doesn't trigger re-pulls (the hash
# is stable, so Tier 2 short-circuits forever).
#
# # Status (before fix): both tests below FAIL.
# # Result (after fix): both pass. ``from_page_node`` synthesises a
# # component-library section for top-level COMPONENT/COMPONENT_SETs (analogous
# # to the existing ``(Ungrouped)`` synthesis for top-level FRAMEs), and
# # ``compute_page_hash`` includes top-level COMPONENT/COMPONENT_SETs so the
# # hash is meaningful for component-only pages.
#
# Real-world evidence: the linear-git ``❖ Design System`` file has 14 pages
# with ``md_path: null`` in the manifest. 9 of them have ``page_hash:
# "4f53cda18c2baa0c"`` (the empty-list digest) and ``component_md_paths: []``
# despite being published-component pages per ``_census.md``. Until this is
# fixed, the user's stated goal — "Bart's new design system fully
# articulated, with all design tokens and components" — is unreachable.
"""

from __future__ import annotations

from figmaclaw.figma_hash import compute_page_hash
from figmaclaw.figma_models import from_page_node


def _component_set_node(*, node_id: str, name: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "type": "COMPONENT_SET",
        "children": [
            {
                "id": f"{node_id}:default",
                "name": "Default",
                "type": "COMPONENT",
                "children": [],
            }
        ],
    }


def _tooltip_help_icon_page_node() -> dict:
    """Mirror the real ❖ Design System ✅ Tooltip & Help icon page shape."""
    return {
        "id": "1478:11585",
        "name": "✅ Tooltip & Help icon",
        "type": "CANVAS",
        "children": [
            _component_set_node(node_id="1478:11586", name="Tooltip"),
            _component_set_node(node_id="1478:12000", name="Help icon"),
        ],
    }


def test_from_page_node_picks_up_top_level_component_sets() -> None:
    page = from_page_node(
        _tooltip_help_icon_page_node(),
        file_key="AZswXfXwfx2fff3RFBMo8h",
        file_name="❖ Design System",
    )

    component_section_names = {s.name for s in page.sections if s.is_component_library}
    assert component_section_names, (
        "from_page_node produced ZERO component sections for a page whose "
        "top-level children are COMPONENT_SETs. Real designers place "
        "components directly on the canvas without a SECTION wrapper, so "
        "this drop is a silent partial-pull bug. See agent-A H2."
    )

    # The synthesised section should expose the actual component nodes so
    # downstream rendering can write a component .md.
    rendered_frame_ids = {
        f.node_id for s in page.sections if s.is_component_library for f in s.frames
    }
    assert "1478:11586" in rendered_frame_ids, rendered_frame_ids
    assert "1478:12000" in rendered_frame_ids, rendered_frame_ids


def test_compute_page_hash_changes_when_top_level_components_change() -> None:
    """The page hash must depend on top-level COMPONENT/COMPONENT_SETs.

    Otherwise editing or adding a top-level component leaves the hash
    constant (the empty-list digest), Tier 2 of the refresh ladder
    short-circuits, and the page is never re-pulled. This is exactly the
    failure mode observed across 9 pages of the linear-git design system.
    """
    base = _tooltip_help_icon_page_node()
    hash_with_two = compute_page_hash(base)

    # Drop one of the two top-level component sets — must produce a different hash.
    one_component = {**base, "children": base["children"][:1]}
    hash_with_one = compute_page_hash(one_component)
    assert hash_with_one != hash_with_two, (
        f"compute_page_hash returned the same value ({hash_with_one!r}) when "
        "a top-level COMPONENT_SET was added/removed. This is what makes "
        "partial-pull pages stick: Tier 2 sees the same hash forever."
    )

    # Empty page (no children at all) should still hash to a stable value,
    # but it MUST NOT collide with a page that has top-level component sets.
    empty_page = {**base, "children": []}
    hash_empty = compute_page_hash(empty_page)
    assert hash_empty != hash_with_one, hash_empty
    assert hash_empty != hash_with_two, hash_empty


def test_invisible_top_level_component_set_is_skipped() -> None:
    """An invisible top-level COMPONENT_SET must not appear in any section.

    Designers sometimes hide work-in-progress components. The
    ``is_visible`` filter must apply before the new top-level component
    handler too — otherwise we'd render hidden components and produce a
    .md the designer didn't want.
    """
    page_node = {
        "id": "1478:11585",
        "name": "✅ Tooltip & Help icon",
        "type": "CANVAS",
        "children": [
            {**_component_set_node(node_id="1478:11586", name="Hidden tooltip"), "visible": False},
            _component_set_node(node_id="1478:12000", name="Visible help icon"),
        ],
    }
    page = from_page_node(
        page_node,
        file_key="AZswXfXwfx2fff3RFBMo8h",
        file_name="❖ Design System",
    )
    rendered_ids = {f.node_id for s in page.sections if s.is_component_library for f in s.frames}
    assert "1478:11586" not in rendered_ids, (
        "Invisible top-level COMPONENT_SET leaked into the rendered "
        "sections. Visibility must be the very first filter applied."
    )
    assert "1478:12000" in rendered_ids


def test_mixed_top_level_frames_and_component_sets_produce_both_sections() -> None:
    """Real-world pages can have both top-level FRAMEs (screen content)
    and top-level COMPONENT_SETs (library components). Each must produce
    its own synthetic section — the existing ``(Ungrouped)`` for frames
    and the new ``(Ungrouped components)`` for components — not collapse
    into one classification.
    """
    page_node = {
        "id": "1478:11585",
        "name": "Mixed page",
        "type": "CANVAS",
        "children": [
            {
                "id": "1478:11700",
                "name": "Top-level frame",
                "type": "FRAME",
                "children": [],
            },
            _component_set_node(node_id="1478:11800", name="Inline component set"),
        ],
    }
    page = from_page_node(
        page_node,
        file_key="AZswXfXwfx2fff3RFBMo8h",
        file_name="❖ Design System",
    )

    screen_sections = [s for s in page.sections if not s.is_component_library]
    component_sections = [s for s in page.sections if s.is_component_library]
    assert any("1478:11700" in {f.node_id for f in s.frames} for s in screen_sections), (
        "Top-level FRAME was not classified as a screen section."
    )
    assert any("1478:11800" in {f.node_id for f in s.frames} for s in component_sections), (
        "Top-level COMPONENT_SET was not classified as a component-library "
        "section even though a top-level FRAME also exists on the page."
    )


def test_top_level_component_only_page_round_trips_through_pull_shape() -> None:
    """A page with only COMPONENT_SETs at the top level must end up with
    at least one component section, so the manifest entry has either an
    md_path or non-empty component_md_paths — i.e. it cannot be the
    "partial-pull" shape (md_path=None AND component_md_paths=[]) that
    the user has been tripping over for 10+ rebuild cycles."""
    page = from_page_node(
        _tooltip_help_icon_page_node(),
        file_key="AZswXfXwfx2fff3RFBMo8h",
        file_name="❖ Design System",
    )

    has_screen = any(not s.is_component_library and s.frames for s in page.sections)
    has_components = any(s.is_component_library and s.frames for s in page.sections)
    assert has_screen or has_components, (
        "Page with top-level COMPONENT_SETs produced neither screen nor "
        "component sections. Pull will write a manifest entry with "
        "md_path=null AND component_md_paths=[] — the exact partial-pull "
        "shape we are trying to eliminate."
    )
