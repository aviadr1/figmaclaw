"""Render a FigmaPage into a semantic markdown navigation index.

Two render targets:

render_page() — for screen pages (figma/*/pages/*.md)
  - YAML frontmatter with machine-readable metadata (FigmaPageFrontmatter schema)
  - H1 header, Figma deep link, optional page summary
  - Per-section tables (Screen | Node ID | Description) for screen sections only
  - Mermaid flowchart from prototype reactions + LLM-inferred flows
  - Quick Reference table
  - Component library sections are omitted — they get their own files via render_component_section()

render_component_section() — for component library sections (figma/*/components/*.md)
  - YAML frontmatter with section_node_id for direct Figma navigation
  - H1 header: {file} / {page} / {section}
  - Variants table: Variant | Node ID | Description

Policy: all structured data needed by machines lives in the YAML frontmatter.
        The table rows and prose are for human/AI reading only — never parse them.
"""

from __future__ import annotations

import yaml

from figmaclaw.figma_frontmatter import FigmaclawMeta, FigmaPageFrontmatter
from figmaclaw.figma_models import FigmaPage, FigmaSection
from figmaclaw.figma_sync_state import PageEntry

_PLACEHOLDER = "(no description yet)"


def render_page(page: FigmaPage, entry: PageEntry) -> str:
    """Render screen sections of a FigmaPage to semantic markdown with YAML frontmatter.

    Component library sections are skipped — use render_component_section() for those.
    """
    parts: list[str] = []

    # Only render non-component sections
    screen_sections = [s for s in page.sections if not s.is_component_library]

    # Collect frame descriptions for frontmatter (screen sections only)
    frame_descs: dict[str, str] = {
        frame.node_id: frame.description
        for section in screen_sections
        for frame in section.frames
        if frame.description
    }

    frontmatter = FigmaPageFrontmatter(
        figmaclaw=FigmaclawMeta(
            file_key=page.file_key,
            page_node_id=page.page_node_id,
            page_hash=entry.page_hash,
        ),
        frames=frame_descs,
        flows=[[src, dst] for src, dst in page.flows],
    )

    fm_dict = frontmatter.model_dump(exclude_none=True)
    if not fm_dict.get("frames"):
        fm_dict.pop("frames", None)
    if not fm_dict.get("flows"):
        fm_dict.pop("flows", None)
    parts.append("---")
    parts.append(yaml.dump(fm_dict, default_flow_style=False, allow_unicode=True).rstrip())
    parts.append("---")
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

    # Per-section tables (screen sections only)
    for section in screen_sections:
        parts.append(f"## {section.name} (`{section.node_id}`)")
        parts.append("")
        parts.append("| Screen | Node ID | Description |")
        parts.append("|--------|---------|-------------|")
        for frame in section.frames:
            desc = frame.description if frame.description else _PLACEHOLDER
            parts.append(f"| {frame.name} | `{frame.node_id}` | {desc} |")
        parts.append("")

    # Mermaid flowchart — from prototype reactions + LLM-inferred flows
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

    # Quick Reference table (screen sections only)
    parts.append("## Quick Reference")
    parts.append("")
    parts.append("| Screen | Node ID | Section | Description |")
    parts.append("|--------|---------|---------|-------------|")
    for section in screen_sections:
        for frame in section.frames:
            desc = frame.description if frame.description else _PLACEHOLDER
            parts.append(f"| {frame.name} | `{frame.node_id}` | {section.name} | {desc} |")
    parts.append("")

    return "\n".join(parts)


def render_component_section(
    section: FigmaSection,
    page: FigmaPage,
    page_hash: str,
) -> str:
    """Render a component library section to a standalone semantic markdown file.

    Output: figma/{file-slug}/components/{section-slug}.md
    Format: YAML frontmatter + title + variants table (no flows, no Mermaid).
    """
    parts: list[str] = []

    # Collect component descriptions for frontmatter
    frame_descs: dict[str, str] = {
        f.node_id: f.description
        for f in section.frames
        if f.description
    }

    frontmatter = FigmaPageFrontmatter(
        figmaclaw=FigmaclawMeta(
            file_key=page.file_key,
            page_node_id=page.page_node_id,
            section_node_id=section.node_id,
            page_hash=page_hash,
        ),
        frames=frame_descs,
    )

    fm_dict = frontmatter.model_dump(exclude_none=True)
    if not fm_dict.get("frames"):
        fm_dict.pop("frames", None)
    if not fm_dict.get("flows"):
        fm_dict.pop("flows", None)
    parts.append("---")
    parts.append(yaml.dump(fm_dict, default_flow_style=False, allow_unicode=True).rstrip())
    parts.append("---")
    parts.append("")

    # H1: file / page / section — unambiguous for cross-page lookups
    parts.append(f"# {page.file_name} / {page.page_name} / {section.name}")
    parts.append("")

    # Deep link to the section node in Figma
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
        desc = frame.description if frame.description else _PLACEHOLDER
        parts.append(f"| {frame.name} | `{frame.node_id}` | {desc} |")
    parts.append("")

    return "\n".join(parts)


def _mermaid_id(node_id: str) -> str:
    """Convert a node ID like '11:1' to a safe Mermaid identifier."""
    return "n" + node_id.replace(":", "_")
