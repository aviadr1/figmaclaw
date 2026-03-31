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
