"""Render a FigmaPage into a semantic markdown navigation index.

Two render targets:

render_page() — for screen pages (figma/*/pages/*.md)
  - YAML frontmatter: flat file_key/page_node_id + compact flow-style frames/flows
  - H1 header, Figma deep link, optional page summary paragraph
  - Per-section: optional intro sentence + table (Screen | Node ID | Description)
  - Mermaid flowchart from prototype reactions + LLM-inferred flows
  - Component library sections are omitted — they get their own files

render_component_section() — for component library sections (figma/*/components/*.md)
  - YAML frontmatter: flat file_key/page_node_id/section_node_id + compact frames
  - H1 header: {file} / {page} / {section}
  - Variants table: Variant | Node ID | Description

DESIGN CONTRACT — body vs frontmatter:
  - Frontmatter = machine-readable source of truth. CI reads/writes this.
    Use it to determine WHAT needs updating (which frames changed, new flows, etc).
  - Body = human/LLM-readable prose: page summary, section intros, frame tables,
    Mermaid charts. Updated ONLY by the figma-enrich-page skill via LLM.
  - render_page() writes a skeleton body for new pages (placeholder descriptions).
    For existing pages with LLM prose, body updates go through the skill, not here.
  - NEVER parse prose from the body. No code should extract page_summary, section
    intros, or any other prose from the markdown body. The LLM receives the whole
    body as-is alongside new Figma data and rewrites it intelligently.

Frontmatter format: top-level block YAML for scalar fields; flow style (single-line)
for frames dict and flows list, using FlowDict/FlowList wrappers + _FrontmatterDumper.
"""

from __future__ import annotations

import yaml

from figmaclaw.figma_models import FigmaPage, FigmaSection
from figmaclaw.figma_sync_state import PageEntry

# NOTE: skill docs (figma-enrich-page.md) reference this string — update them if changed.
PLACEHOLDER = "(no description yet)"


# --- Flow-style YAML helpers ---

class _FlowDict(dict):
    """dict subclass rendered as a single-line YAML flow mapping."""


class _FlowList(list):
    """list subclass rendered as a single-line YAML flow sequence."""


class _FrontmatterDumper(yaml.Dumper):
    """YAML dumper that forces FlowDict/FlowList to single-line flow style."""


_FrontmatterDumper.add_representer(
    _FlowDict,
    lambda dumper, data: dumper.represent_mapping(
        "tag:yaml.org,2002:map", data, flow_style=True
    ),
)
_FrontmatterDumper.add_representer(
    _FlowList,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    ),
)


def _build_frontmatter(
    file_key: str,
    page_node_id: str,
    section_node_id: str | None,
    frame_descs: dict[str, str],
    flows: list[tuple[str, str]],
) -> str:
    """Render compact YAML frontmatter block (between --- markers)."""
    fm: dict = {"file_key": file_key, "page_node_id": page_node_id}
    if section_node_id:
        fm["section_node_id"] = section_node_id
    if frame_descs:
        fm["frames"] = _FlowDict(frame_descs)
    if flows:
        fm["flows"] = _FlowList([[src, dst] for src, dst in flows])

    body = yaml.dump(
        fm,
        Dumper=_FrontmatterDumper,
        default_flow_style=False,
        allow_unicode=True,
        width=2**20,  # prevent PyYAML from wrapping long flow-style values
    ).rstrip()
    return f"---\n{body}\n---"


def render_page(page: FigmaPage, entry: PageEntry) -> str:
    """Render screen sections of a FigmaPage to semantic markdown with YAML frontmatter.

    Component library sections are skipped — use render_component_section() for those.
    """
    parts: list[str] = []

    screen_sections = [s for s in page.sections if not s.is_component_library]

    frame_descs: dict[str, str] = {
        frame.node_id: frame.description
        for section in screen_sections
        for frame in section.frames
    }

    parts.append(_build_frontmatter(
        file_key=page.file_key,
        page_node_id=page.page_node_id,
        section_node_id=None,
        frame_descs=frame_descs,
        flows=page.flows,
    ))
    parts.append("")

    # H1 header
    parts.append(f"# {page.file_name} / {page.page_name}")
    parts.append("")

    # Figma URL
    parts.append(f"[Open in Figma]({page.figma_url})")
    parts.append("")

    # Page summary (LLM-generated)
    if page.page_summary:
        parts.append(page.page_summary)
        parts.append("")

    # Per-section: optional intro + table.
    # NOTE: figma_md_parse._SECTION_RE and _FRAME_ROW_RE are coupled to this format:
    #   section header: "## {name} (`{node_id}`)"
    #   frame row:      "| {name} | `{node_id}` | {desc} |"
    # Keep those patterns in sync if either format changes.
    for section in screen_sections:
        parts.append(f"## {section.name} (`{section.node_id}`)")
        parts.append("")
        if section.intro:
            parts.append(section.intro)
            parts.append("")
        parts.append("| Screen | Node ID | Description |")
        parts.append("|--------|---------|-------------|")
        for frame in section.frames:
            desc = frame.description if frame.description else PLACEHOLDER
            parts.append(f"| {frame.name} | `{frame.node_id}` | {desc} |")
        parts.append("")

    # Mermaid flowchart
    if page.flows:
        node_labels: dict[str, str] = {
            frame.node_id: frame.name
            for section in screen_sections
            for frame in section.frames
        }
        parts.append("## Screen Flow")
        parts.append("")
        parts.append("```mermaid")
        parts.append("flowchart LR")
        for src, dst in page.flows:
            src_label = node_labels.get(src, src)
            dst_label = node_labels.get(dst, dst)
            parts.append(
                f'    {_mermaid_id(src)}["{src_label}"] --> {_mermaid_id(dst)}["{dst_label}"]'
            )
        parts.append("```")
        parts.append("")

    return "\n".join(parts)


def render_component_section(
    section: FigmaSection,
    page: FigmaPage,
) -> str:
    """Render a component library section to a standalone semantic markdown file.

    Output: figma/{file-slug}/components/{section-slug}.md
    Format: YAML frontmatter + title + variants table (no flows, no Mermaid).
    """
    parts: list[str] = []

    frame_descs: dict[str, str] = {
        f.node_id: f.description
        for f in section.frames
    }

    parts.append(_build_frontmatter(
        file_key=page.file_key,
        page_node_id=page.page_node_id,
        section_node_id=section.node_id,
        frame_descs=frame_descs,
        flows=[],
    ))
    parts.append("")

    # H1: file / page / section
    parts.append(f"# {page.file_name} / {page.page_name} / {section.name}")
    parts.append("")

    # Deep link to the section node
    section_url = (
        f"https://www.figma.com/design/{page.file_key}"
        f"?node-id={section.node_id.replace(':', '-')}"
    )
    parts.append(f"[Open in Figma]({section_url})")
    parts.append("")

    # Variants table
    parts.append(f"## Variants (`{section.node_id}`)")
    parts.append("")
    parts.append("| Variant | Node ID | Description |")
    parts.append("|---------|---------|-------------|")
    for frame in section.frames:
        desc = frame.description if frame.description else PLACEHOLDER
        parts.append(f"| {frame.name} | `{frame.node_id}` | {desc} |")
    parts.append("")

    return "\n".join(parts)


def _mermaid_id(node_id: str) -> str:
    """Convert a node ID like '11:1' to a safe Mermaid identifier."""
    return "n" + node_id.replace(":", "_")
