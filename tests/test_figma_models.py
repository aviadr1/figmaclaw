"""Tests for figma_models.py.

INVARIANTS:
- CONNECTOR nodes are filtered out of frame lists
- Ungrouped top-level FRAMEs collected into an (Ungrouped) section
- SECTION nodes become FigmaSection, their FRAME children become FigmaFrame
- FigmaPage built from a CANVAS node has correct metadata
- Prototype reactions on frames produce flow edges
"""

from __future__ import annotations

import pytest

from figmaclaw.figma_models import FigmaFile, FigmaFrame, FigmaPage, FigmaSection, from_page_node


def _canvas(node_id: str, name: str, children: list[dict]) -> dict:
    return {"id": node_id, "name": name, "type": "CANVAS", "children": children}


def _section(node_id: str, name: str, children: list[dict]) -> dict:
    return {"id": node_id, "name": name, "type": "SECTION", "children": children}


def _frame(node_id: str, name: str, reactions: list[dict] | None = None) -> dict:
    node: dict = {"id": node_id, "name": name, "type": "FRAME", "children": []}
    if reactions:
        node["reactions"] = reactions
    return node


def _connector(node_id: str) -> dict:
    return {"id": node_id, "name": "Connector line", "type": "CONNECTOR", "children": []}


def _reaction(dest_id: str) -> dict:
    return {
        "trigger": {"type": "ON_CLICK"},
        "action": {
            "type": "NODE",
            "destinationId": dest_id,
            "navigation": "NAVIGATE",
        },
    }


def test_section_nodes_become_figma_sections():
    """INVARIANT: SECTION nodes at the page level each become a FigmaSection."""
    page = _canvas("0:1", "My Page", [
        _section("10:1", "sign up flow", [_frame("11:1", "welcome screen")]),
        _section("10:2", "login flow", [_frame("12:1", "login screen")]),
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    assert len(result.sections) == 2
    assert result.sections[0].name == "sign up flow"
    assert result.sections[1].name == "login flow"


def test_frame_children_of_sections_become_figma_frames():
    """INVARIANT: FRAME children of SECTION nodes become FigmaFrame instances."""
    page = _canvas("0:1", "My Page", [
        _section("10:1", "onboarding", [
            _frame("11:1", "welcome"),
            _frame("11:2", "permissions"),
        ])
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    frames = result.sections[0].frames
    assert len(frames) == 2
    assert frames[0].node_id == "11:1"
    assert frames[0].name == "welcome"
    assert frames[1].node_id == "11:2"
    assert frames[1].name == "permissions"


def test_connector_nodes_are_filtered_out():
    """INVARIANT: CONNECTOR nodes must not appear as FigmaFrame instances."""
    page = _canvas("0:1", "My Page", [
        _section("10:1", "flows", [
            _frame("11:1", "screen A"),
            _connector("11:99"),
            _frame("11:2", "screen B"),
        ])
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    frame_names = [f.name for f in result.sections[0].frames]
    assert "Connector line" not in frame_names
    assert len(frame_names) == 2


def test_ungrouped_top_level_frames_go_into_ungrouped_section():
    """INVARIANT: Top-level FRAME nodes (no parent SECTION) appear in (Ungrouped) section."""
    page = _canvas("0:1", "My Page", [
        _frame("11:1", "floating frame"),
        _frame("11:2", "another frame"),
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    assert len(result.sections) == 1
    assert result.sections[0].name == "(Ungrouped)"
    assert len(result.sections[0].frames) == 2


def test_mixed_sections_and_ungrouped_frames():
    """INVARIANT: Named sections and ungrouped frames coexist correctly."""
    page = _canvas("0:1", "My Page", [
        _section("10:1", "auth", [_frame("11:1", "login")]),
        _frame("12:1", "orphan frame"),
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    section_names = [s.name for s in result.sections]
    assert "auth" in section_names
    assert "(Ungrouped)" in section_names


def test_prototype_reactions_produce_flow_edges():
    """INVARIANT: Prototype NAVIGATE reactions produce (source_name, dest_id) flow edges."""
    page = _canvas("0:1", "My Page", [
        _section("10:1", "onboarding", [
            _frame("11:1", "welcome", reactions=[_reaction("11:2")]),
            _frame("11:2", "permissions"),
        ])
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    assert len(result.flows) == 1
    assert result.flows[0] == ("11:1", "11:2")


def test_no_reactions_means_empty_flows():
    """INVARIANT: Pages with no prototype links have an empty flows list."""
    page = _canvas("0:1", "My Page", [
        _section("10:1", "auth", [_frame("11:1", "login"), _frame("11:2", "home")]),
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    assert result.flows == []


def test_page_metadata_is_populated():
    """INVARIANT: FigmaPage carries file_key, file_name, page_node_id, page_name."""
    page = _canvas("7741:45837", "Reach - auto content sharing", [])
    result = from_page_node(page, file_key="hOV4QM", file_name="Web App")
    assert result.file_key == "hOV4QM"
    assert result.file_name == "Web App"
    assert result.page_node_id == "7741:45837"
    assert result.page_name == "Reach - auto content sharing"


def test_figma_frame_is_pydantic_model():
    """INVARIANT: FigmaFrame is a Pydantic BaseModel (not dataclass)."""
    import pydantic
    assert issubclass(FigmaFrame, pydantic.BaseModel)


def test_figma_section_is_pydantic_model():
    """INVARIANT: FigmaSection is a Pydantic BaseModel."""
    import pydantic
    assert issubclass(FigmaSection, pydantic.BaseModel)


def test_figma_page_is_pydantic_model():
    """INVARIANT: FigmaPage is a Pydantic BaseModel."""
    import pydantic
    assert issubclass(FigmaPage, pydantic.BaseModel)


def test_figma_file_is_pydantic_model():
    """INVARIANT: FigmaFile is a Pydantic BaseModel."""
    import pydantic
    assert issubclass(FigmaFile, pydantic.BaseModel)


def test_empty_page_has_no_sections():
    """INVARIANT: A page with no children produces no sections."""
    page = _canvas("0:1", "Empty Page", [])
    result = from_page_node(page, file_key="abc", file_name="App")
    assert result.sections == []


def _component_set(node_id: str, name: str) -> dict:
    return {"id": node_id, "name": name, "type": "COMPONENT_SET", "children": []}


def _component(node_id: str, name: str) -> dict:
    return {"id": node_id, "name": name, "type": "COMPONENT", "children": []}


def test_section_with_component_sets_is_flagged_as_component_library():
    """INVARIANT: A SECTION whose children are COMPONENT_SET nodes is a component library."""
    page = _canvas("0:1", "Design System", [
        _section("10:1", "Buttons", [
            _component_set("20:1", "Button / Primary"),
            _component_set("20:2", "Button / Secondary"),
        ])
    ])
    result = from_page_node(page, file_key="ds", file_name="Design System")
    assert result.sections[0].is_component_library is True


def test_section_with_frames_is_not_component_library():
    """INVARIANT: A SECTION with FRAME children is not a component library."""
    page = _canvas("0:1", "My Page", [
        _section("10:1", "Screens", [_frame("11:1", "Home"), _frame("11:2", "Settings")])
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    assert result.sections[0].is_component_library is False


def test_component_library_section_lists_component_nodes_as_frames():
    """INVARIANT: Component nodes in a library section are exposed as FigmaFrame instances."""
    page = _canvas("0:1", "DS", [
        _section("10:1", "Icons", [
            _component("20:1", "icon / star"),
            _component("20:2", "icon / heart"),
        ])
    ])
    result = from_page_node(page, file_key="ds", file_name="DS")
    section = result.sections[0]
    assert section.is_component_library is True
    assert len(section.frames) == 2
    assert section.frames[0].name == "icon / star"
    assert section.frames[1].name == "icon / heart"


def test_section_with_both_frames_and_components_prefers_frames():
    """INVARIANT: Mixed sections (frames + components) use frames and are not flagged as library."""
    page = _canvas("0:1", "Mixed", [
        _section("10:1", "mixed section", [
            _frame("11:1", "some screen"),
            _component_set("20:1", "Button"),
        ])
    ])
    result = from_page_node(page, file_key="abc", file_name="App")
    section = result.sections[0]
    assert section.is_component_library is False
    # Only the frame should appear (frames take priority)
    assert len(section.frames) == 1
    assert section.frames[0].name == "some screen"


def test_figma_section_is_component_library_defaults_to_false():
    """INVARIANT: FigmaSection.is_component_library defaults to False."""
    section = FigmaSection(node_id="1:1", name="Normal Section")
    assert section.is_component_library is False


def test_hidden_frames_excluded():
    """INVARIANT: Frames with visible=false are filtered out of the page model.

    Hidden frames can't be rendered by the Figma image export API (returns null URL).
    Including them causes infinite loops in enrichment (screenshots returns nothing,
    but the frame stays as '(no description yet)' forever).
    """
    canvas = _canvas("0:1", "Page", [
        _frame("1:1", "Visible frame"),
        {**_frame("1:2", "Hidden frame"), "visible": False},
        _section("2:1", "My Section", [
            _frame("3:1", "Visible in section"),
            {**_frame("3:2", "Hidden in section"), "visible": False},
        ]),
    ])
    page = from_page_node(canvas, file_key="abc123", file_name="File")

    all_frame_ids = [f.node_id for s in page.sections for f in s.frames]
    assert "1:1" in all_frame_ids, "Visible top-level frame should be included"
    assert "1:2" not in all_frame_ids, "Hidden top-level frame must be excluded"
    assert "3:1" in all_frame_ids, "Visible frame in section should be included"
    assert "3:2" not in all_frame_ids, "Hidden frame in section must be excluded"


def test_hidden_section_excludes_all_children_inherited_visibility():
    """INVARIANT: Hiding a SECTION in Figma hides all its descendants,
    regardless of the descendants' own visibility flags.

    This matches Figma's canvas rendering: clicking the eye icon on a
    parent hides the whole subtree. A visible frame inside a hidden
    section must NOT appear in the rendered page model.
    """
    canvas = _canvas("0:1", "Page", [
        _frame("1:1", "Top visible"),
        {
            **_section("2:1", "Hidden section", [
                _frame("3:1", "Visible frame inside hidden section"),
                _frame("3:2", "Another"),
            ]),
            "visible": False,
        },
        _section("4:1", "Visible section", [
            _frame("5:1", "Visible in visible section"),
        ]),
    ])
    page = from_page_node(canvas, file_key="abc123", file_name="File")

    all_frame_ids = {f.node_id for s in page.sections for f in s.frames}
    assert "1:1" in all_frame_ids
    assert "5:1" in all_frame_ids
    assert "3:1" not in all_frame_ids, (
        "Frame inside a hidden SECTION must be excluded (inherited visibility)"
    )
    assert "3:2" not in all_frame_ids

    # The hidden section itself must NOT appear in the model.
    section_ids = {s.node_id for s in page.sections}
    assert "2:1" not in section_ids, "Hidden SECTION must be dropped entirely"
    assert "4:1" in section_ids
