"""Render a FigmaPage into a semantic markdown navigation index.

The output format is designed for AI navigation:
- H1 header with file + page name
- HTML comment with machine-readable metadata
- Per-section tables (Screen | Node ID | Description)
- Optional Mermaid flowchart for prototype flows
- Quick Reference table

No YAML frontmatter — purely human+AI readable markdown.
"""

from __future__ import annotations

from figmaclaw.figma_models import FigmaPage
from figmaclaw.figma_sync_state import PageEntry

_PLACEHOLDER = "(no description yet)"


def render_page(page: FigmaPage, entry: PageEntry) -> str:
    """Render a FigmaPage to semantic markdown."""
    lines: list[str] = []

    # H1 header
    lines.append(f"# {page.file_name} / {page.page_name}")
    lines.append("")

    # HTML comment with machine-readable metadata (line 3)
    lines.append(
        f"<!-- figmaclaw: file_key={page.file_key}"
        f" page_node_id={page.page_node_id}"
        f" page_hash={entry.page_hash} -->"
    )
    lines.append("")

    # Figma URL
    lines.append(f"[Open in Figma]({page.figma_url})")
    lines.append("")

    # Per-section tables
    for section in page.sections:
        lines.append(f"## {section.name} (`{section.node_id}`)")
        lines.append("")
        lines.append("| Screen | Node ID | Description |")
        lines.append("|--------|---------|-------------|")
        for frame in section.frames:
            desc = frame.description if frame.description else _PLACEHOLDER
            lines.append(f"| {frame.name} | `{frame.node_id}` | {desc} |")
        lines.append("")

    # Optional Mermaid flowchart
    if page.flows:
        # Build node label map from all frames
        node_labels: dict[str, str] = {}
        for section in page.sections:
            for frame in section.frames:
                node_labels[frame.node_id] = frame.name

        lines.append("## Prototype Flows")
        lines.append("")
        lines.append("```mermaid")
        lines.append("flowchart LR")
        for src, dst in page.flows:
            src_label = node_labels.get(src, src)
            dst_label = node_labels.get(dst, dst)
            lines.append(f'    {_mermaid_id(src)}["{src_label}"] --> {_mermaid_id(dst)}["{dst_label}"]')
        lines.append("```")
        lines.append("")

    # Quick Reference table
    lines.append("## Quick Reference")
    lines.append("")
    lines.append("| Screen | Node ID | Section | Description |")
    lines.append("|--------|---------|---------|-------------|")
    for section in page.sections:
        for frame in section.frames:
            desc = frame.description if frame.description else _PLACEHOLDER
            lines.append(f"| {frame.name} | `{frame.node_id}` | {section.name} | {desc} |")
    lines.append("")

    return "\n".join(lines)


def _mermaid_id(node_id: str) -> str:
    """Convert a node ID like '11:1' to a safe Mermaid identifier."""
    return "n" + node_id.replace(":", "_")
