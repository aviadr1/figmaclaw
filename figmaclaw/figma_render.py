"""Render a FigmaPage into a semantic markdown navigation index.

Output format:
- YAML frontmatter with machine-readable metadata (FigmaPageFrontmatter schema)
- H1 header with file + page name
- Figma deep link
- Per-section tables (Screen | Node ID | Description) — human-readable display
- Optional Mermaid flowchart for prototype flows
- Quick Reference table

Policy: all structured data needed by machines lives in the YAML frontmatter.
        The table rows are for human/AI reading only — never parse them.
"""

from __future__ import annotations

import yaml

from figmaclaw.figma_frontmatter import FigmaclawMeta, FigmaPageFrontmatter
from figmaclaw.figma_models import FigmaPage
from figmaclaw.figma_sync_state import PageEntry

_PLACEHOLDER = "(no description yet)"


def render_page(page: FigmaPage, entry: PageEntry) -> str:
    """Render a FigmaPage to semantic markdown with YAML frontmatter."""
    parts: list[str] = []

    # Collect frame descriptions for frontmatter
    frame_descs: dict[str, str] = {
        frame.name: frame.description
        for section in page.sections
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
    )

    fm_dict = frontmatter.model_dump()
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

    # Per-section tables
    for section in page.sections:
        parts.append(f"## {section.name} (`{section.node_id}`)")
        parts.append("")
        parts.append("| Screen | Node ID | Description |")
        parts.append("|--------|---------|-------------|")
        for frame in section.frames:
            desc = frame.description if frame.description else _PLACEHOLDER
            parts.append(f"| {frame.name} | `{frame.node_id}` | {desc} |")
        parts.append("")

    # Optional Mermaid flowchart
    if page.flows:
        node_labels: dict[str, str] = {
            frame.node_id: frame.name
            for section in page.sections
            for frame in section.frames
        }
        parts.append("## Prototype Flows")
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

    # Quick Reference table
    parts.append("## Quick Reference")
    parts.append("")
    parts.append("| Screen | Node ID | Section | Description |")
    parts.append("|--------|---------|---------|-------------|")
    for section in page.sections:
        for frame in section.frames:
            desc = frame.description if frame.description else _PLACEHOLDER
            parts.append(f"| {frame.name} | `{frame.node_id}` | {section.name} | {desc} |")
    parts.append("")

    return "\n".join(parts)


def _mermaid_id(node_id: str) -> str:
    """Convert a node ID like '11:1' to a safe Mermaid identifier."""
    return "n" + node_id.replace(":", "_")
