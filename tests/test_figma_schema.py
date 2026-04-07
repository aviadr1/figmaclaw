"""Tests for ``figmaclaw.figma_schema`` — the canonical schema module.

These tests express the **invariants** that every other part of the pipeline
depends on. If any of these break, file readers and writers will drift and
data will be silently dropped (see figmaclaw#25).

INVARIANTS proved here:

SCH-1  normalize_name is idempotent.
SCH-2  normalize_name maps None, "", and whitespace-only to UNNAMED.
SCH-3  render_section_heading refuses empty node_id.
SCH-4  render_frame_row refuses empty node_id.
SCH-5  render_section_heading produces a heading that parse_section_heading
       round-trips for any input (including empty / whitespace-only names).
SCH-6  render_frame_row produces a row that parse_frame_row round-trips for
       any (name, node_id) pair.
SCH-7  parse_section_heading distinguishes frame sections (non-empty node_id)
       from prose sections (empty node_id).
SCH-8  parse_frame_row rejects table separators and header rows.
SCH-9  Visibility predicate: explicit False → hidden; anything else → visible.
SCH-10 Renderable child predicate: (structural or component) AND visible.
SCH-11 The ORIGINAL figmaclaw#25 regression: a heading written as
       ``##  (`id`)`` (empty name, two spaces) parses to node_id="id",
       name=UNNAMED — NOT to node_id="", which would drop every frame
       beneath it.
"""

from __future__ import annotations

import pytest

from figmaclaw.figma_schema import (
    PLACEHOLDER_DESCRIPTION,
    SCREEN_FLOW_SECTION,
    UNGROUPED_NODE_ID,
    UNGROUPED_SECTION,
    UNNAMED,
    FrameRow,
    SectionHeading,
    assert_frame_row_round_trip,
    assert_section_round_trip,
    is_component,
    is_h2,
    is_placeholder_row,
    is_renderable_child,
    is_structural,
    is_table_separator,
    is_visible,
    normalize_name,
    parse_frame_row,
    parse_section_heading,
    raw_name,
    render_frame_row,
    render_frame_table_header,
    render_prose_heading,
    render_section_heading,
    render_variant_table_header,
)


# ---------------------------------------------------------------------------
# SCH-1 / SCH-2 — name normalization.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("Foo", "Foo"),
        ("Foo Bar", "Foo Bar"),
        ("  Foo  ", "Foo"),  # stripped
        ("🧠 Rationale", "🧠 Rationale"),  # unicode preserved
        ("", UNNAMED),
        (" ", UNNAMED),  # single space
        ("   ", UNNAMED),  # multiple spaces
        ("\t\n", UNNAMED),  # other whitespace
        (None, UNNAMED),
    ],
)
def test_normalize_name(given: str | None, expected: str) -> None:
    """SCH-2: empty/whitespace → UNNAMED; otherwise stripped."""
    assert normalize_name(given) == expected


@pytest.mark.parametrize(
    "given",
    ["Foo", "Foo Bar", "", " ", None, "🧠 Rationale", "  padded  "],
)
def test_normalize_name_is_idempotent(given: str | None) -> None:
    """SCH-1: normalize is a fixed-point function after one application."""
    once = normalize_name(given)
    twice = normalize_name(once)
    assert once == twice


# ---------------------------------------------------------------------------
# SCH-3 / SCH-4 — render input validation.
# ---------------------------------------------------------------------------


def test_render_section_heading_rejects_empty_node_id() -> None:
    """SCH-3: a section heading MUST have a node_id."""
    with pytest.raises(ValueError, match="node_id"):
        render_section_heading("Foo", "")


def test_render_frame_row_rejects_empty_node_id() -> None:
    """SCH-4: a frame row MUST have a node_id."""
    with pytest.raises(ValueError, match="node_id"):
        render_frame_row("Foo", "", "description")


def test_render_prose_heading_rejects_empty_name() -> None:
    """Prose headings (Screen Flow) must have a real name."""
    with pytest.raises(ValueError, match="name"):
        render_prose_heading("")
    with pytest.raises(ValueError, match="name"):
        render_prose_heading("   ")


def test_render_prose_heading_returns_stripped_name() -> None:
    """Prose heading output has the canonical ``## <name>`` form."""
    assert render_prose_heading("Screen Flow") == "## Screen Flow"
    assert render_prose_heading("  My Notes  ") == "## My Notes"


def test_parse_section_heading_bare_h2_with_only_whitespace() -> None:
    """A heading like ``## `` (two hashes + space + nothing) is a legal if
    unusual H2. We parse it as a prose heading with empty name, empty
    node_id — callers can ignore or report as malformed as they wish."""
    parsed = parse_section_heading("## ")
    assert parsed is not None
    assert parsed.name == ""
    assert parsed.node_id == ""
    assert parsed.is_frame_section is False


# ---------------------------------------------------------------------------
# SCH-5 — section heading render/parse round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,node_id",
    [
        ("Foo", "10:1"),
        ("Foo Bar", "10:1"),
        ("", "10:1"),  # THE figmaclaw#25 CASE
        (" ", "10:1"),  # whitespace-only name
        ("   ", "10:1"),
        (None, "10:1"),
        ("🧠 Rationale", "10:1"),
        ("(Ungrouped)", "ungrouped"),  # synthetic node_id
        ("Name with (parens)", "10:1"),
        ("Name with `backticks`", "10:1"),
    ],
)
def test_section_heading_round_trip(name: str | None, node_id: str) -> None:
    """SCH-5: render_section_heading is injective; parse_section_heading inverts it."""
    assert_section_round_trip(name, node_id)


def test_section_heading_empty_name_renders_as_unnamed() -> None:
    """When we render an empty-name section, the output shows ``(Unnamed)``,
    not two spaces. This is the user-facing half of the figmaclaw#25 fix.
    """
    rendered = render_section_heading("", "13957:220538")
    assert rendered == "## (Unnamed) (`13957:220538`)"


def test_section_heading_none_name_renders_as_unnamed() -> None:
    rendered = render_section_heading(None, "13957:220538")
    assert rendered == "## (Unnamed) (`13957:220538`)"


# ---------------------------------------------------------------------------
# SCH-6 — frame row round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,node_id,description",
    [
        ("Welcome", "11:1", "A welcome screen"),
        ("Welcome", "11:1", ""),  # empty description = placeholder slot
        ("", "11:1", "description"),  # empty name normalized
        (" ", "11:1", "description"),  # whitespace-only
        (None, "11:1", "description"),
        ("Frame with | pipe", "11:1", "Description with | pipe"),  # escaping
        ("🧠 Rationale", "11:1", "emoji"),
        ("Name", "11:1", PLACEHOLDER_DESCRIPTION),
    ],
)
def test_frame_row_round_trip(name: str | None, node_id: str, description: str) -> None:
    """SCH-6: render_frame_row and parse_frame_row form a bijection on (name, node_id)."""
    assert_frame_row_round_trip(name, node_id, description)


def test_frame_row_escapes_pipes_in_description() -> None:
    """Pipes in descriptions must be escaped so the table stays well-formed."""
    rendered = render_frame_row("Foo", "1:2", "a | b | c")
    # The row must parse as a valid row (pipes in desc escaped to \|)
    parsed = parse_frame_row(rendered)
    assert parsed is not None
    assert parsed.node_id == "1:2"
    # And the escaped form must appear in the rendered output
    assert "\\|" in rendered


# ---------------------------------------------------------------------------
# SCH-7 — frame section vs prose section disambiguation.
# ---------------------------------------------------------------------------


def test_parse_section_heading_frame_section() -> None:
    """Sections with ``(`id`)`` suffix are frame sections."""
    s = parse_section_heading("## Onboarding (`10:1`)")
    assert s is not None
    assert s.name == "Onboarding"
    assert s.node_id == "10:1"
    assert s.is_frame_section is True


def test_parse_section_heading_prose_section() -> None:
    """Sections without ``(`id`)`` are prose sections."""
    s = parse_section_heading("## Screen Flow")
    assert s is not None
    assert s.name == "Screen Flow"
    assert s.node_id == ""
    assert s.is_frame_section is False


def test_parse_section_heading_not_an_h2() -> None:
    """Non-H2 lines return None."""
    assert parse_section_heading("# Page Title") is None
    assert parse_section_heading("### Sub") is None
    assert parse_section_heading("plain text") is None
    assert parse_section_heading("| table | row |") is None


def test_parse_section_heading_empty_name_preserves_node_id() -> None:
    """SCH-11: the figmaclaw#25 regression.

    An old-format heading written with an empty name and two spaces must
    still parse back to the correct node_id, with the name normalized to
    ``(Unnamed)``. This is what lets files created before the fix heal
    themselves on the next enrichment pass.
    """
    s = parse_section_heading("##  (`13957:220538`)")
    assert s is not None
    assert s.node_id == "13957:220538", (
        "node_id must be extracted even when name is empty — "
        "the whole figmaclaw#25 bug was that this returned empty node_id"
    )
    assert s.name == UNNAMED
    assert s.is_frame_section is True


def test_parse_section_heading_whitespace_name_preserves_node_id() -> None:
    """Three-space-name heading also recovers cleanly."""
    s = parse_section_heading("##    (`13957:220538`)")
    assert s is not None
    assert s.node_id == "13957:220538"
    assert s.name == UNNAMED


# ---------------------------------------------------------------------------
# SCH-8 — frame row edge cases.
# ---------------------------------------------------------------------------


def test_parse_frame_row_rejects_separator() -> None:
    """Table separators are not frame rows."""
    assert parse_frame_row("|---|---|---|") is None
    assert parse_frame_row("|--------|---------|-------------|") is None
    assert parse_frame_row("| --- | --- | --- |") is None


def test_parse_frame_row_rejects_header_row() -> None:
    """Header rows like ``| Screen | Node ID | Description |`` have no
    backticked node_id in the second column, so they must NOT be parsed
    as frame rows."""
    assert parse_frame_row("| Screen | Node ID | Description |") is None
    assert parse_frame_row("| Variant | Node ID | Description |") is None


def test_parse_frame_row_valid() -> None:
    r = parse_frame_row("| Welcome | `11:1` | a description |")
    assert r == FrameRow(name="Welcome", node_id="11:1")


def test_parse_frame_row_empty_name_normalized() -> None:
    r = parse_frame_row("|  | `11:1` | a description |")
    assert r is not None
    assert r.name == UNNAMED
    assert r.node_id == "11:1"


def test_parse_frame_row_non_table_line() -> None:
    assert parse_frame_row("Just some text") is None
    assert parse_frame_row("") is None
    assert parse_frame_row("## Section (`1:2`)") is None


# ---------------------------------------------------------------------------
# SCH-8b — table separator predicate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "|---|---|---|",
        "|-----|-----|-----|",
        "| --- | --- | --- |",
        "|--------|---------|-------------|",
    ],
)
def test_is_table_separator_positive(line: str) -> None:
    assert is_table_separator(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "| Screen | Node ID | Description |",
        "| Welcome | `11:1` | desc |",
        "## Section (`10:1`)",
        "",
        "plain text",
    ],
)
def test_is_table_separator_negative(line: str) -> None:
    assert is_table_separator(line) is False


def test_is_placeholder_row_detects_placeholder() -> None:
    row = render_frame_row("Frame", "1:2", PLACEHOLDER_DESCRIPTION)
    assert is_placeholder_row(row) is True


def test_is_placeholder_row_rejects_real_description() -> None:
    row = render_frame_row("Frame", "1:2", "a real description")
    assert is_placeholder_row(row) is False


def test_is_h2_positive_and_negative() -> None:
    assert is_h2("## anything") is True
    assert is_h2("## ") is True  # bare ``## `` is still H2; empty name is a separate concern
    assert is_h2("##foo") is False  # no space — malformed, not an H2
    assert is_h2("#  not h2") is False
    assert is_h2("### h3") is False
    assert is_h2("") is False


# ---------------------------------------------------------------------------
# SCH-9 / SCH-10 — ingestion predicates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node,expected",
    [
        ({}, True),  # missing field → visible
        ({"visible": True}, True),
        ({"visible": False}, False),  # the one negative case
        ({"visible": None}, True),  # None is not False → visible
    ],
)
def test_is_visible(node: dict, expected: bool) -> None:
    assert is_visible(node) is expected


@pytest.mark.parametrize(
    "type_,expected",
    [
        ("FRAME", True),
        ("SECTION", True),
        ("COMPONENT", False),
        ("TEXT", False),
        ("", False),
    ],
)
def test_is_structural(type_: str, expected: bool) -> None:
    assert is_structural({"type": type_}) is expected


@pytest.mark.parametrize(
    "type_,expected",
    [
        ("COMPONENT", True),
        ("COMPONENT_SET", True),
        ("FRAME", False),
        ("TEXT", False),
    ],
)
def test_is_component(type_: str, expected: bool) -> None:
    assert is_component({"type": type_}) is expected


def test_is_renderable_child_requires_type_and_visibility() -> None:
    """SCH-10: the predicate that decides if a Figma child becomes a row."""
    assert is_renderable_child({"type": "FRAME", "visible": True}) is True
    assert is_renderable_child({"type": "FRAME"}) is True  # default visible
    assert is_renderable_child({"type": "FRAME", "visible": False}) is False
    assert is_renderable_child({"type": "COMPONENT"}) is True
    assert is_renderable_child({"type": "COMPONENT", "visible": False}) is False
    assert is_renderable_child({"type": "TEXT"}) is False
    assert is_renderable_child({"type": "VECTOR"}) is False
    assert is_renderable_child({"type": "CONNECTOR"}) is False


def test_raw_name_handles_missing_and_none() -> None:
    assert raw_name({"name": "Foo"}) == "Foo"
    assert raw_name({"name": ""}) == ""
    assert raw_name({"name": None}) == ""  # guards against None from API
    assert raw_name({}) == ""


# ---------------------------------------------------------------------------
# Table header rendering.
# ---------------------------------------------------------------------------


def test_render_frame_table_header_shape() -> None:
    header, sep = render_frame_table_header()
    assert header.startswith("| Screen ")
    assert "Node ID" in header
    assert is_table_separator(sep)


def test_render_variant_table_header_shape() -> None:
    header, sep = render_variant_table_header()
    assert header.startswith("| Variant ")
    assert "Node ID" in header
    assert is_table_separator(sep)


# ---------------------------------------------------------------------------
# Integration: a whole section round-trips through render → parse.
# ---------------------------------------------------------------------------


def test_render_parse_whole_section_with_empty_section_name() -> None:
    """End-to-end test against the figmaclaw#25 data shape.

    Renders a whole section with an empty name and three frames, parses it
    back, and verifies every frame is recoverable.
    """
    heading = render_section_heading("", "13957:220538")
    header, sep = render_frame_table_header()
    rows = [
        render_frame_row("Create community", "13957:220539", PLACEHOLDER_DESCRIPTION),
        render_frame_row("Create community", "13957:220573", PLACEHOLDER_DESCRIPTION),
        render_frame_row("Buttons Mobile", "13957:221441", PLACEHOLDER_DESCRIPTION),
    ]
    md = "\n".join([heading, "", header, sep, *rows, ""])

    # Parse heading
    parsed_heading = parse_section_heading(md.splitlines()[0])
    assert parsed_heading is not None
    assert parsed_heading.name == UNNAMED
    assert parsed_heading.node_id == "13957:220538"

    # Parse every row
    parsed_rows = [parse_frame_row(line) for line in md.splitlines()]
    valid = [r for r in parsed_rows if r is not None]
    assert len(valid) == 3
    assert {r.node_id for r in valid} == {"13957:220539", "13957:220573", "13957:221441"}
    # And every one has a non-empty name (normalized if needed)
    assert all(r.name for r in valid)


# ---------------------------------------------------------------------------
# Module-level constants sanity.
# ---------------------------------------------------------------------------


def test_constants_are_stable() -> None:
    """Downstream code and prompts reference these strings by value —
    changing them requires updating prompts, so this test is a tripwire.
    """
    assert PLACEHOLDER_DESCRIPTION == "(no description yet)"
    assert UNNAMED == "(Unnamed)"
    assert UNGROUPED_SECTION == "(Ungrouped)"
    assert UNGROUPED_NODE_ID == "ungrouped"
    assert SCREEN_FLOW_SECTION == "Screen Flow"
