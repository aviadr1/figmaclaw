"""Pydantic models for Figma entities.

Ingestion is the single place where raw Figma API data is converted into
figmaclaw's internal model. All schema-level decisions — which node types
render, what counts as visible, how empty names are normalized — are
delegated to :mod:`figmaclaw.figma_schema` so render, parse, and ingestion
can't drift.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from figmaclaw.figma_schema import (
    UNGROUPED_COMPONENTS_NODE_ID,
    UNGROUPED_COMPONENTS_SECTION,
    UNGROUPED_NODE_ID,
    UNGROUPED_SECTION,
    is_component,
    is_structural,
    is_visible,
    normalize_name,
    raw_name,
)


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


class Webhook(BaseModel):
    """A Figma file-level webhook as returned by the v2 webhooks API.

    Only the fields this project cares about are modelled; unknown fields
    (e.g. team_id on team-scoped webhooks) are ignored.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    context_id: str
    endpoint: str
    status: str = "ACTIVE"


class ValidationReport(BaseModel):
    """Result of checking the exactly-one-webhook-per-file invariant."""

    missing: list[str] = Field(default_factory=list)
    duplicates: list[tuple[str, list[Webhook]]] = Field(default_factory=list)
    stale: list[Webhook] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.duplicates or self.stale)


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
        name=normalize_name(raw_name(node)),
        figma_url=f"https://www.figma.com/design/{file_key}?node-id={node_id.replace(':', '-')}",
    )


def from_page_node(page_node: dict, *, file_key: str, file_name: str) -> FigmaPage:
    """Build a FigmaPage from a raw Figma CANVAS node dict.

    Tree traversal rules:

    * ``SECTION`` nodes at the top level → :class:`FigmaSection` with
      visible FRAME children.
    * ``FRAME`` nodes at the top level (not inside a SECTION) → collected
      into a synthetic ``(Ungrouped)`` section.
    * ``CONNECTOR``, ``TEXT``, ``VECTOR`` and other non-visual nodes →
      filtered out via :func:`figma_schema.is_structural` /
      :func:`figma_schema.is_component`.

    Every name (page, section, frame) is normalized at ingestion via
    :func:`figma_schema.normalize_name` so downstream render/parse code
    never sees empty or whitespace-only names.
    """
    page_node_id: str = page_node["id"]
    page_name: str = normalize_name(raw_name(page_node))
    children: list[dict] = page_node.get("children", [])

    sections: list[FigmaSection] = []
    ungrouped_frames: list[FigmaFrame] = []
    # Top-level COMPONENT/COMPONENT_SET nodes that aren't wrapped in a SECTION
    # — collected into a synthetic component-library section below so the
    # page produces a non-empty manifest entry and a component .md, instead
    # of dropping silently with md_path=null and component_md_paths=[].
    # Real Gigaverse pages with this shape: ✅ Tooltip & Help icon,
    # ☼ Logo, ☼ App Icon, ☼ Date & Time Format. See agent-A H2.
    ungrouped_components: list[FigmaFrame] = []
    all_frames_for_flows: list[dict] = []

    for child in children:
        # Inherited visibility: a hidden parent hides all descendants, so
        # we skip the whole subtree before looking at its children. This
        # matches Figma's canvas rendering semantics — hiding a group
        # hides everything underneath it.
        if not is_visible(child):
            continue

        if is_structural(child) and child.get("type") == "SECTION":
            child_children = child.get("children", [])
            # Frames: visible structural FRAME children.
            frame_nodes = [c for c in child_children if c.get("type") == "FRAME" and is_visible(c)]
            # Components: visible COMPONENT / COMPONENT_SET children.
            component_nodes = [c for c in child_children if is_component(c) and is_visible(c)]
            # A SECTION is a component library iff it holds components and
            # no frames. This classification is content-based, not
            # metadata-based.
            is_component_lib = bool(component_nodes) and not frame_nodes
            # Render frames when present; fall back to components for libs.
            render_nodes = frame_nodes if frame_nodes else component_nodes
            all_frames_for_flows.extend(frame_nodes)
            sections.append(
                FigmaSection(
                    node_id=child["id"],
                    name=normalize_name(raw_name(child)),
                    frames=[_node_to_frame(f, file_key) for f in render_nodes],
                    is_component_library=is_component_lib,
                )
            )

        elif child.get("type") == "FRAME":
            # Visibility for FRAMEs already guarded by the outer
            # ``is_visible(child)`` check above.
            all_frames_for_flows.append(child)
            ungrouped_frames.append(_node_to_frame(child, file_key))

        elif is_component(child):
            # Top-level COMPONENT or COMPONENT_SET — no SECTION wrapper.
            # Visibility already guarded above. We treat these as a
            # synthetic component-library section (see UNGROUPED_COMPONENTS_*
            # in figma_schema) so they don't drop silently.
            ungrouped_components.append(_node_to_frame(child, file_key))

        # Every other top-level child type (CONNECTOR, TEXT, VECTOR, ...)
        # is skipped — not rendered to markdown.

    if ungrouped_frames:
        sections.append(
            FigmaSection(
                node_id=UNGROUPED_NODE_ID,
                name=UNGROUPED_SECTION,
                frames=ungrouped_frames,
            )
        )

    if ungrouped_components:
        # Page-scoped synthetic node_id. Without this, every page that has
        # top-level COMPONENT_SETs would produce a section with the same
        # constant node_id (UNGROUPED_COMPONENTS_NODE_ID), and pull_logic's
        # component_path slug computation would collide:
        #   sect_slug = f"{slugify(section.name)}-{section.node_id.replace(':', '-')}"
        # would resolve to a single
        # ``components/ungrouped-components-ungrouped-components.md``
        # for every such page in the file. Last writer wins; previous
        # pages' components are silently overwritten on disk.
        # Encoding the page_node_id keeps each synthetic section uniquely
        # identifiable while still sharing the human-readable name.
        synthetic_id = f"{UNGROUPED_COMPONENTS_NODE_ID}-{page_node_id.replace(':', '-')}"
        sections.append(
            FigmaSection(
                node_id=synthetic_id,
                name=UNGROUPED_COMPONENTS_SECTION,
                frames=ungrouped_components,
                is_component_library=True,
            )
        )

    flows = _extract_flows(all_frames_for_flows)

    figma_url = f"https://www.figma.com/design/{file_key}?node-id={page_node_id.replace(':', '-')}"
    return FigmaPage(
        file_key=file_key,
        file_name=normalize_name(file_name),
        page_node_id=page_node_id,
        page_name=page_name,
        figma_url=figma_url,
        sections=sections,
        flows=flows,
    )
