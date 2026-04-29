"""Adversarial tests hunting bugs around the PR 129 fix surface.

These probe the boundaries of:

* ``from_page_node`` traversal — invisible parents, mixed children,
  duplicate names, real sections that happen to share a synthetic name.
* ``compute_page_hash`` — order independence, invisible-child stability,
  variant-inside-COMPONENT_SET detection (Tier 2 short-circuit gap).
* ``compute_frame_hashes`` — coverage of all rendered units, including
  top-level COMPONENT/COMPONENT_SETs (currently NOT covered, documented
  as a gap below).
* ``slugify`` and ``component_path`` — ensure synthetic component
  sections cannot collide across pages, even with adversarial names.
* ``parse_section_heading`` round-trip for the synthetic
  ``(Ungrouped components)`` section.

Each test pins **one specific behavior** so that a regression points
straight at the broken assumption. Where a test documents a known gap
(rather than asserting a fix), the assertion comments make that
explicit so future engineers don't mistake it for an aspiration.

# Coordinates: agent A, post-fix hardening pass on PR 129.
"""

from __future__ import annotations

import pytest

from figmaclaw.figma_hash import (
    compute_frame_hashes,
    compute_page_hash,
)
from figmaclaw.figma_models import from_page_node
from figmaclaw.figma_paths import component_path, slugify
from figmaclaw.figma_schema import (
    UNGROUPED_COMPONENTS_NODE_ID,
    UNGROUPED_COMPONENTS_SECTION,
    UNGROUPED_NODE_ID,
    UNGROUPED_SECTION,
    parse_section_heading,
    render_section_heading,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _component(node_id: str, name: str = "Default") -> dict:
    return {"id": node_id, "name": name, "type": "COMPONENT", "children": []}


def _component_set(
    node_id: str,
    name: str,
    *,
    variants: list[dict] | None = None,
    visible: bool | None = None,
) -> dict:
    node: dict = {
        "id": node_id,
        "name": name,
        "type": "COMPONENT_SET",
        "children": variants or [_component(f"{node_id}:default", "Default")],
    }
    if visible is not None:
        node["visible"] = visible
    return node


def _frame(node_id: str, name: str, *, visible: bool | None = None) -> dict:
    node: dict = {"id": node_id, "name": name, "type": "FRAME", "children": []}
    if visible is not None:
        node["visible"] = visible
    return node


def _section(
    node_id: str,
    name: str,
    *,
    children: list[dict] | None = None,
    visible: bool | None = None,
) -> dict:
    node: dict = {
        "id": node_id,
        "name": name,
        "type": "SECTION",
        "children": children or [],
    }
    if visible is not None:
        node["visible"] = visible
    return node


def _page(node_id: str, name: str, children: list[dict]) -> dict:
    return {"id": node_id, "name": name, "type": "CANVAS", "children": children}


FILE_KEY = "AZswXfXwfx2fff3RFBMo8h"
FILE_NAME = "❖ Design System"


# ---------------------------------------------------------------------------
# Hash stability — order, invisibility, irrelevant fields
# ---------------------------------------------------------------------------


def test_compute_page_hash_is_order_independent_for_top_level_component_sets() -> None:
    """Sorting children must NOT change the hash. Figma can return children
    in any order and we must not trigger spurious re-pulls because of it."""
    a = _component_set("1:1", "Tooltip")
    b = _component_set("1:2", "Help icon")
    c = _component_set("1:3", "Toast")
    page_one = _page("1:0", "p", [a, b, c])
    page_two = _page("1:0", "p", [c, a, b])
    page_three = _page("1:0", "p", [b, c, a])

    h1 = compute_page_hash(page_one)
    h2 = compute_page_hash(page_two)
    h3 = compute_page_hash(page_three)
    assert h1 == h2 == h3, (
        f"page hash depends on child order: {h1!r} {h2!r} {h3!r} — "
        "this would cause spurious re-pulls every time Figma serializes "
        "children in a different order."
    )


def test_compute_page_hash_ignores_invisible_top_level_component_set_addition() -> None:
    """Adding an *invisible* COMPONENT_SET must not bump the hash.

    Otherwise a designer hiding a WIP component triggers a re-pull and
    re-enrichment cycle even though the rendered output is identical.
    """
    base = _page("1:0", "p", [_component_set("1:1", "Tooltip")])
    augmented = _page(
        "1:0",
        "p",
        [
            _component_set("1:1", "Tooltip"),
            _component_set("1:2", "Hidden WIP", visible=False),
        ],
    )
    assert compute_page_hash(base) == compute_page_hash(augmented), (
        "Adding an invisible COMPONENT_SET changed page_hash. Hidden "
        "nodes are NOT rendered, so the hash must stay stable."
    )


def test_compute_page_hash_ignores_irrelevant_node_fields() -> None:
    """Fills, position, size, locked-state, and rotation must not perturb
    the hash. Only ``id``/``name``/``type``/visibility/parent participate."""
    base = _page("1:0", "p", [_component_set("1:1", "Tooltip")])
    perturbed = _page(
        "1:0",
        "p",
        [
            {
                **_component_set("1:1", "Tooltip"),
                "fills": [{"type": "SOLID", "color": {"r": 1, "g": 0, "b": 0}}],
                "absoluteBoundingBox": {"x": 100, "y": 100, "width": 50, "height": 50},
                "locked": True,
                "rotation": 0.5,
            }
        ],
    )
    assert compute_page_hash(base) == compute_page_hash(perturbed)


def test_compute_page_hash_changes_when_top_level_component_set_renamed() -> None:
    """Renaming a top-level COMPONENT_SET MUST bump the page hash so the
    rendered .md picks up the new heading on next pull."""
    before = _page("1:0", "p", [_component_set("1:1", "Tooltip")])
    after = _page("1:0", "p", [_component_set("1:1", "Tooltip (v2)")])
    assert compute_page_hash(before) != compute_page_hash(after)


def test_compute_page_hash_changes_when_visibility_flips() -> None:
    """A previously-visible COMPONENT_SET being hidden MUST change the hash —
    the rendered output drops it, so the manifest must re-render."""
    visible = _page("1:0", "p", [_component_set("1:1", "Tooltip")])
    hidden = _page("1:0", "p", [_component_set("1:1", "Tooltip", visible=False)])
    assert compute_page_hash(visible) != compute_page_hash(hidden)


# ---------------------------------------------------------------------------
# DOCUMENTED GAP — variant-content-changes inside COMPONENT_SETs
# ---------------------------------------------------------------------------


def test_GAP_adding_variant_inside_top_level_component_set_does_not_change_page_hash() -> None:
    """Documents a known Tier-2 short-circuit gap: adding a new COMPONENT
    *inside* an existing COMPONENT_SET does NOT change the page hash.

    Concretely: ``compute_page_hash`` adds a tuple for the COMPONENT_SET
    itself but never descends into its variants. So an internal variant
    addition is invisible to the refresh ladder. The page is marked
    ``content_unchanged`` (pull_logic.py:1044) and skipped — even though
    the rendered variant table on disk is now stale.

    This test PINS the current behavior so a future fix is forced to
    update the assertion. It is NOT a fix; it is a regression contract
    saying "if you change this, look at pull_logic Tier 2".

    Real-world workaround: any time a designer touches a variant, they
    typically also rename or reorder something at the COMPONENT_SET level,
    or the file's `version` flips and ``last_modified`` advances —
    figmaclaw refetches the file but the per-page short-circuit still
    fires. Surgical fix would be to descend one more level into
    COMPONENT_SETs and include their variants in the hash tuples.
    """
    one_variant = _page(
        "1:0",
        "p",
        [_component_set("1:1", "Toggle", variants=[_component("1:1:on", "On")])],
    )
    two_variants = _page(
        "1:0",
        "p",
        [
            _component_set(
                "1:1",
                "Toggle",
                variants=[_component("1:1:on", "On"), _component("1:1:off", "Off")],
            )
        ],
    )
    h1 = compute_page_hash(one_variant)
    h2 = compute_page_hash(two_variants)
    # PINNING the gap. Flip to `!=` when the hash starts descending into
    # COMPONENT_SETs.
    assert h1 == h2, (
        "page_hash now distinguishes inside-COMPONENT_SET variant changes — "
        "great! Update this test (flip the assertion) and check that "
        "Tier 2 of pull_logic still skips correctly when the COMPONENT_SET "
        "itself is unchanged."
    )


def test_GAP_compute_frame_hashes_skips_top_level_component_sets() -> None:
    """Documents that compute_frame_hashes() returns nothing for top-level
    COMPONENT/COMPONENT_SETs. They are rendered as variant tables in a
    component .md, not as frame rows in a screen .md, so per-frame staleness
    detection does not apply. PINS the behavior so callers don't grow a
    silent dependency on it. If component variant staleness ever needs to
    be tracked, do it through a separate per-section content hash, not by
    smuggling COMPONENT/COMPONENT_SET ids into frame_hashes."""
    page_node = _page(
        "1:0",
        "p",
        [_component_set("1:1", "Tooltip"), _component_set("1:2", "Help icon")],
    )
    hashes = compute_frame_hashes(page_node)
    assert hashes == {}, (
        f"compute_frame_hashes returned non-empty {hashes!r} for a "
        "component-only page. This is an API change — confirm that "
        "stale_frame_ids and downstream consumers handle component ids."
    )


# ---------------------------------------------------------------------------
# Synthetic section uniqueness across pages
# ---------------------------------------------------------------------------


def test_synthetic_component_section_path_unique_across_two_real_pages() -> None:
    """End-to-end: simulate the path that pull_logic computes for the
    synthetic component section on two distinct pages. The resulting
    component .md paths MUST be different — otherwise the second page's
    write silently overwrites the first.

    This is the production bug we hit (Logo + App Icon both wrote to
    ``components/ungrouped-components-ungrouped-components.md``). The
    fix encodes ``page_node_id`` into the synthetic node_id; this test
    pins that encoding via the path layer, not just via in-memory ids."""
    page_a = from_page_node(
        _page("83:38162", "☼ Logo", [_component_set("83:38163", "logo")]),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    page_b = from_page_node(
        _page("500:23", "☼ App Icon", [_component_set("500:24", "app-icon")]),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )

    file_slug = "design-system"

    def synth_path(page) -> str:
        sect = next(s for s in page.sections if s.is_component_library)
        sect_suffix = sect.node_id.replace(":", "-")
        sect_slug = f"{slugify(sect.name)}-{sect_suffix}"
        return component_path(file_slug, sect_slug)

    p1 = synth_path(page_a)
    p2 = synth_path(page_b)
    assert p1 != p2, (
        f"two pages share the same synthetic component .md path: {p1!r}. "
        "The second page would silently overwrite the first."
    )


def test_synthetic_section_round_trips_through_render_parse() -> None:
    """The synthetic ``(Ungrouped components)`` section name + its
    page-scoped node_id must round-trip through render_section_heading /
    parse_section_heading. Otherwise the body parser would reject the
    H2 line and downstream enrichment would skip it."""
    page = from_page_node(
        _page("83:38162", "☼ Logo", [_component_set("83:38163", "logo")]),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    sect = next(s for s in page.sections if s.is_component_library)
    rendered = render_section_heading(sect.name, sect.node_id)
    parsed = parse_section_heading(rendered)
    assert parsed is not None, rendered
    assert parsed.name == sect.name
    assert parsed.node_id == sect.node_id


# ---------------------------------------------------------------------------
# Adversarial naming — collisions and unicode
# ---------------------------------------------------------------------------


def test_two_top_level_component_sets_with_same_name_keep_distinct_node_ids() -> None:
    """If two top-level COMPONENT_SETs on the same page share a name
    (e.g. designer accident), both still get rendered as distinct rows.
    We never deduplicate by name."""
    page = from_page_node(
        _page(
            "1:0",
            "p",
            [_component_set("1:1", "Toggle"), _component_set("1:2", "Toggle")],
        ),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    sect = next(s for s in page.sections if s.is_component_library)
    ids = [f.node_id for f in sect.frames]
    assert ids == ["1:1", "1:2"], ids


def test_section_named_ungrouped_components_does_not_collide_with_synthetic() -> None:
    """Pathological case: a designer literally names a SECTION
    ``(Ungrouped components)``. The synthetic and the real should remain
    distinct because they have different node_ids."""
    real_sect = _section(
        "1:1",
        UNGROUPED_COMPONENTS_SECTION,
        children=[_component_set("1:2", "Real toggle")],
    )
    page = from_page_node(
        _page(
            "1:0",
            "p",
            [real_sect, _component_set("1:3", "Top-level tooltip")],
        ),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    component_sections = [s for s in page.sections if s.is_component_library]
    node_ids = sorted(s.node_id for s in component_sections)
    # Real one keeps its Figma id (1:1). Synthetic one is page-scoped.
    expected_synthetic = f"{UNGROUPED_COMPONENTS_NODE_ID}-1-0"
    assert "1:1" in node_ids, node_ids
    assert expected_synthetic in node_ids, node_ids
    assert len(node_ids) == 2, node_ids


def test_section_named_ungrouped_does_not_collide_with_synthetic_frame_section() -> None:
    """Mirror of the above for the screen-side ``(Ungrouped)`` synthetic.

    A SECTION literally named ``(Ungrouped)`` plus a top-level FRAME
    must produce two distinct sections, both classified as screen
    sections, with distinct node_ids."""
    real_sect = _section(
        "1:1",
        UNGROUPED_SECTION,
        children=[_frame("1:2", "Real screen")],
    )
    page = from_page_node(
        _page(
            "1:0",
            "p",
            [real_sect, _frame("1:3", "Top-level frame")],
        ),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    screen_sections = [s for s in page.sections if not s.is_component_library]
    node_ids = sorted(s.node_id for s in screen_sections)
    assert "1:1" in node_ids, node_ids
    assert UNGROUPED_NODE_ID in node_ids, node_ids
    assert len(node_ids) == 2, node_ids


def test_unicode_emoji_page_name_slugifies_safely() -> None:
    """A page like ``☼ Logo`` should slugify to a usable URL slug. We
    don't pin the exact output (slugify is private) but we do require
    it to be non-empty and not contain whitespace/emoji."""
    slug = slugify("☼ Logo")
    assert slug, "emoji-only or emoji-prefixed name slugified to empty"
    assert " " not in slug
    assert "☼" not in slug


def test_page_with_only_emoji_name_falls_back_to_fallback() -> None:
    """If a name reduces to nothing after slugification, the fallback
    must be used so the file path is still legal."""
    assert slugify("☼ ❖ ✅", fallback="untitled") == "untitled"


# ---------------------------------------------------------------------------
# Visibility cascading
# ---------------------------------------------------------------------------


def test_invisible_section_with_visible_components_inside_drops_everything() -> None:
    """A SECTION marked ``visible: false`` hides everything underneath it,
    even children that explicitly set ``visible: true``. Inherited
    visibility — Figma's canvas semantics."""
    page_node = _page(
        "1:0",
        "p",
        [
            _section(
                "1:1",
                "Hidden section",
                children=[_component_set("1:2", "Inner toggle")],
                visible=False,
            ),
            _component_set("1:3", "Visible toggle"),
        ],
    )
    page = from_page_node(page_node, file_key=FILE_KEY, file_name=FILE_NAME)
    rendered = {f.node_id for s in page.sections for f in s.frames}
    assert "1:2" not in rendered, "visible child of an invisible section leaked into render"
    assert "1:3" in rendered

    # And the hash must NOT include the hidden subtree.
    h_with_hidden = compute_page_hash(page_node)
    h_without_hidden = compute_page_hash(
        _page("1:0", "p", [_component_set("1:3", "Visible toggle")])
    )
    assert h_with_hidden == h_without_hidden, (
        "page_hash leaked invisible subtree contents — would cause "
        "spurious re-pulls on visibility-only changes."
    )


def test_top_level_component_with_no_variants_still_renders_as_frame_row() -> None:
    """An empty COMPONENT_SET (no children) at the top level must still
    produce a frame entry. Otherwise a designer who created a placeholder
    set without variants would have it silently disappear from the .md
    after pull."""
    page = from_page_node(
        _page("1:0", "p", [{"id": "1:1", "name": "Empty set", "type": "COMPONENT_SET"}]),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    sect = next(s for s in page.sections if s.is_component_library)
    assert any(f.node_id == "1:1" for f in sect.frames)


# ---------------------------------------------------------------------------
# Mixed-shape pages
# ---------------------------------------------------------------------------


def test_section_with_both_frames_and_components_classifies_as_screen() -> None:
    """When a SECTION has BOTH FRAMEs and COMPONENT_SETs, the existing
    rule (figma_models.py: ``is_component_lib = bool(component_nodes)
    and not frame_nodes``) classifies it as a screen section. Pin the
    rule so future contributors don't accidentally invert it."""
    sect = _section(
        "1:1",
        "Mixed",
        children=[_frame("1:2", "Real frame"), _component_set("1:3", "Component on the side")],
    )
    page = from_page_node(
        _page("1:0", "p", [sect]),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    matched = [s for s in page.sections if s.node_id == "1:1"]
    assert len(matched) == 1
    assert not matched[0].is_component_library, (
        "Section with mixed FRAME + COMPONENT children was classified as "
        "a component library. The rule is: a section is a component lib "
        "iff it has components AND zero frames."
    )
    rendered_ids = {f.node_id for f in matched[0].frames}
    # Frames win: only the FRAME is rendered as a row, not the COMPONENT_SET.
    assert "1:2" in rendered_ids
    assert "1:3" not in rendered_ids


def test_top_level_orphan_component_without_set_is_surfaced() -> None:
    """A top-level COMPONENT (not wrapped in COMPONENT_SET) — rare in
    practice but legal in Figma — must still surface in the synthetic
    component-library section. Same partial-pull risk applies."""
    page = from_page_node(
        _page("1:0", "p", [_component("1:1", "Orphan component")]),
        file_key=FILE_KEY,
        file_name=FILE_NAME,
    )
    sect = next(s for s in page.sections if s.is_component_library)
    assert any(f.node_id == "1:1" for f in sect.frames)


# ---------------------------------------------------------------------------
# Pull-shape sanity for purely-empty pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "page_node",
    [
        _page("1:0", "Empty page", []),
        _page("1:0", "All hidden", [_component_set("1:1", "X", visible=False)]),
        _page("1:0", "Only structural noise", [{"id": "1:1", "type": "CONNECTOR"}]),
    ],
    ids=["zero-children", "all-hidden-components", "only-non-renderable"],
)
def test_pages_with_no_visible_renderables_produce_zero_sections(page_node: dict) -> None:
    """Three different shapes of "nothing to render" all collapse to
    sections=[]. They are correctly indistinguishable at the rendered
    output level — and the page_hash distinguishes them at the data
    level."""
    page = from_page_node(page_node, file_key=FILE_KEY, file_name=FILE_NAME)
    assert page.sections == []


def test_empty_and_all_hidden_pages_have_distinct_hashes_from_visible_page() -> None:
    """Empty and all-hidden produce the same hash (correct — both render
    nothing). A page with one VISIBLE COMPONENT_SET must have a distinct
    hash. This is the property that prevents the partial-pull bug from
    re-emerging: making content visible MUST change the hash so Tier 2
    re-pulls."""
    empty = compute_page_hash(_page("1:0", "p", []))
    all_hidden = compute_page_hash(_page("1:0", "p", [_component_set("1:1", "X", visible=False)]))
    visible = compute_page_hash(_page("1:0", "p", [_component_set("1:1", "X")]))
    # Empty == all-hidden is intentional (both render nothing), but both
    # must differ from a page with visible content.
    assert empty == all_hidden, (
        "different hashes for two indistinguishable-render pages — "
        "would cause spurious re-pulls when visibility flips on/off "
        "around a single component."
    )
    assert visible != empty, (
        "visible and empty pages share the same hash — Tier 2 cannot "
        "detect a freshly-added component. This is the partial-pull "
        "regression we shipped PR 129 to fix."
    )
