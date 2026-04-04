"""Pydantic models for Figma entities."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FigmaFrame(BaseModel):
    """A single screen/frame within a section."""

    node_id: str
    name: str
    description: str = ""
    figma_url: str = ""


class FigmaSection(BaseModel):
    """A named grouping of frames within a page."""

    node_id: str
    name: str
    frames: list[FigmaFrame] = Field(default_factory=list)
    is_component_library: bool = False
    intro: str = ""  # LLM-generated one-sentence section intro


class FigmaPage(BaseModel):
    """One Figma page — maps to one .md file."""

    file_key: str
    file_name: str
    page_node_id: str
    page_name: str
    page_slug: str = ""
    figma_url: str = ""
    sections: list[FigmaSection] = Field(default_factory=list)
    flows: list[tuple[str, str]] = Field(default_factory=list)
    page_summary: str = ""
    last_modified: str = ""
    version: str = ""


class FigmaFile(BaseModel):
    """Metadata about a tracked Figma file."""

    file_key: str
    file_name: str
    version: str
    last_modified: str
    pages: list[FigmaPage] = Field(default_factory=list)


_SKIP_NODE_TYPES = frozenset({"CONNECTOR", "TEXT", "VECTOR", "STAR", "LINE", "ELLIPSE", "BOOLEAN_OPERATION"})
_COMPONENT_TYPES = frozenset({"COMPONENT_SET", "COMPONENT"})


def _extract_flows(frames: list[dict]) -> list[tuple[str, str]]:
    """Extract prototype NAVIGATE reactions as (source_node_id, dest_node_id) pairs."""
    flows: list[tuple[str, str]] = []
    for frame in frames:
        for reaction in frame.get("reactions", []):
            action = reaction.get("action", {})
            dest_id = action.get("destinationId")
            nav = action.get("navigation", "")
            if dest_id and nav == "NAVIGATE":
                flows.append((frame["id"], dest_id))
    return flows


def _node_to_frame(node: dict, file_key: str) -> FigmaFrame:
    node_id = node["id"]
    return FigmaFrame(
        node_id=node_id,
        name=node.get("name", ""),
        figma_url=f"https://www.figma.com/design/{file_key}?node-id={node_id.replace(':', '-')}",
    )


def from_page_node(page_node: dict, *, file_key: str, file_name: str) -> FigmaPage:
    """Build a FigmaPage from a raw Figma CANVAS node dict.

    Tree traversal rules:
    - SECTION nodes at the top level → FigmaSection (with FRAME children)
    - FRAME nodes at the top level (not inside a SECTION) → (Ungrouped) section
    - CONNECTOR and other non-visual nodes → filtered out
    """
    page_node_id: str = page_node["id"]
    page_name: str = page_node.get("name", "")
    children: list[dict] = page_node.get("children", [])

    sections: list[FigmaSection] = []
    ungrouped_frames: list[FigmaFrame] = []
    all_frames_for_flows: list[dict] = []

    for child in children:
        child_type = child.get("type", "")

        if child_type == "SECTION":
            child_children = child.get("children", [])
            frame_nodes = [c for c in child_children if c.get("type") == "FRAME" and c.get("visible", True) is not False]
            component_nodes = [c for c in child_children if c.get("type") in _COMPONENT_TYPES and c.get("visible", True) is not False]
            # Component library: section has components but no frames
            is_component_lib = bool(component_nodes) and not frame_nodes
            # Render nodes: frames take priority; fall back to component nodes for libs
            render_nodes = frame_nodes if frame_nodes else component_nodes
            all_frames_for_flows.extend(frame_nodes)
            sections.append(FigmaSection(
                node_id=child["id"],
                name=child.get("name", ""),
                frames=[_node_to_frame(f, file_key) for f in render_nodes],
                is_component_library=is_component_lib,
            ))

        elif child_type == "FRAME" and child.get("visible", True) is not False:
            all_frames_for_flows.append(child)
            ungrouped_frames.append(_node_to_frame(child, file_key))

        # Skip CONNECTOR and other non-visual types at the top level

    if ungrouped_frames:
        sections.append(FigmaSection(
            node_id="ungrouped",
            name="(Ungrouped)",
            frames=ungrouped_frames,
        ))

    flows = _extract_flows(all_frames_for_flows)

    figma_url = f"https://www.figma.com/design/{file_key}?node-id={page_node_id.replace(':', '-')}"
    return FigmaPage(
        file_key=file_key,
        file_name=file_name,
        page_node_id=page_node_id,
        page_name=page_name,
        figma_url=figma_url,
        sections=sections,
        flows=flows,
    )
