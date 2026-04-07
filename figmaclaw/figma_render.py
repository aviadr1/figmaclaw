"""Render a FigmaPage into a semantic markdown navigation index.

Two render targets:

scaffold_page() — for screen pages (figma/*/pages/*.md)
  - YAML frontmatter: flat file_key/page_node_id + compact flow-style frames/flows
  - H1 header, Figma deep link, LLM placeholder for page summary
  - Per-section: LLM placeholder for intro + table (Screen | Node ID | Description)
  - Mermaid flowchart placeholder from prototype reactions
  - Component library sections are omitted — they get their own files
  - Writes initial skeleton for NEW pages only. Existing pages are updated via
    the figma-enrich-page skill (LLM) — never by scaffold_page().

render_component_section() — for component library sections (figma/*/components/*.md)
  - YAML frontmatter: flat file_key/page_node_id/section_node_id + compact frames
  - H1 header: {file} / {page} / {section}
  - Variants table: Variant | Node ID | Description

build_page_frontmatter() — build YAML frontmatter string from a FigmaPage's screen
  sections, for use when updating only the frontmatter of an existing file.

DESIGN CONTRACT — body vs frontmatter:
  - Frontmatter = machine-readable source of truth. CI reads/writes this.
    Use it to determine WHAT needs updating (which frames changed, new flows, etc).
  - Body = human/LLM-readable prose: page summary, section intros, frame tables,
    Mermaid charts. Updated ONLY by the figma-enrich-page skill via LLM.
  - scaffold_page() writes a skeleton body for new pages (with LLM placeholders).
    For existing pages with LLM prose, body updates go through the skill, not here.
  - NEVER parse prose from the body. No code should extract page_summary, section
    intros, or any other prose from the markdown body. The LLM receives the whole
    body as-is alongside new Figma data and rewrites it intelligently.

Frontmatter format: top-level block YAML for scalar fields; flow style (single-line)
for frames dict and flows list, using FlowDict/FlowList wrappers + _FrontmatterDumper.
"""

from __future__ import annotations

import yaml

from figmaclaw.figma_frontmatter import FrameComposition
from figmaclaw.figma_models import FigmaPage, FigmaSection
from figmaclaw.figma_schema import (
    PLACEHOLDER_DESCRIPTION,
    SCREEN_FLOW_SECTION,
    VARIANTS_SECTION,
    normalize_name,
    render_frame_row,
    render_frame_table_header,
    render_prose_heading,
    render_section_heading,
    render_variant_table_header,
)
from figmaclaw.figma_sync_state import PageEntry

# Backward-compat alias — :data:`figma_schema.PLACEHOLDER_DESCRIPTION` is the
# canonical constant, but external callers (skill docs, downstream code) may
# still import ``PLACEHOLDER`` from this module. NOTE: skill docs
# (figma-enrich-page.md) reference this string — update them if changed.
PLACEHOLDER = PLACEHOLDER_DESCRIPTION


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
    frame_ids: list[str],
    flows: list[tuple[str, str]],
    *,
    enriched_hash: str | None = None,
    enriched_at: str | None = None,
    enriched_frame_hashes: dict[str, str] | None = None,
    component_set_keys: dict[str, str] | None = None,
    raw_frames: dict[str, FrameComposition] | None = None,
) -> str:
    """Render compact YAML frontmatter block (between --- markers)."""
    fm: dict = {"file_key": file_key, "page_node_id": page_node_id}
    if section_node_id:
        fm["section_node_id"] = section_node_id
    if frame_ids:
        fm["frames"] = _FlowList(frame_ids)
    if flows:
        fm["flows"] = _FlowList([[src, dst] for src, dst in flows])
    if enriched_hash is not None:
        fm["enriched_hash"] = enriched_hash
    if enriched_at is not None:
        fm["enriched_at"] = enriched_at
    if enriched_frame_hashes:
        fm["enriched_frame_hashes"] = _FlowDict(enriched_frame_hashes)
    if component_set_keys:
        fm["component_set_keys"] = _FlowDict(component_set_keys)
    if raw_frames:
        fm["raw_frames"] = _FlowDict({
            k: _FlowDict({"raw": v.raw, "ds": _FlowList(v.ds)})
            for k, v in raw_frames.items()
        })

    body = yaml.dump(
        fm,
        Dumper=_FrontmatterDumper,
        default_flow_style=False,
        allow_unicode=True,
        width=2**20,  # prevent PyYAML from wrapping long flow-style values
    ).rstrip()
    return f"---\n{body}\n---"


def build_page_frontmatter(
    page: FigmaPage,
    *,
    enriched_hash: str | None = None,
    enriched_at: str | None = None,
    enriched_frame_hashes: dict[str, str] | None = None,
    raw_frames: dict[str, FrameComposition] | None = None,
) -> str:
    """Build the YAML frontmatter block for a screen page from a FigmaPage model.

    Returns the full frontmatter string including ``---`` delimiters. Used by
    update_page_frontmatter() to replace frontmatter without touching the body.

    raw_frames: sparse dict of frames with raw (non-INSTANCE) children, computed
    by the pull pass. Only frames with raw > 0 are included. None means not yet
    computed (field is omitted from frontmatter); {} means computed but all clean.
    """
    screen_sections = [s for s in page.sections if not s.is_component_library]
    frame_ids: list[str] = [
        frame.node_id
        for section in screen_sections
        for frame in section.frames
    ]
    return _build_frontmatter(
        file_key=page.file_key,
        page_node_id=page.page_node_id,
        section_node_id=None,
        frame_ids=frame_ids,
        flows=page.flows,
        enriched_hash=enriched_hash,
        enriched_at=enriched_at,
        enriched_frame_hashes=enriched_frame_hashes,
        raw_frames=raw_frames,
    )


def scaffold_page(
    page: FigmaPage,
    entry: PageEntry,
    *,
    raw_frames: dict[str, FrameComposition] | None = None,
) -> str:
    """Generate a skeleton markdown page with LLM placeholders for a NEW FigmaPage.

    Component library sections are skipped — use render_component_section() for those.

    The scaffold contains explicit ``<!-- LLM: ... -->`` placeholders telling the LLM
    exactly what to fill in. This is used in two ways:

    1. **Write mode** (new file): caller writes the returned string to disk as the
       initial .md file. The LLM fills in placeholders on the next enrich-page run.
    2. **Hint mode** (existing file changed structure): caller prints the scaffold to
       stdout so the LLM can see the expected structure alongside the existing body
       and rewrite the body to match the new structure.

    NEVER call this on an existing file to overwrite it — that destroys LLM prose.
    Use update_page_frontmatter() to update frontmatter of existing files.
    """
    parts: list[str] = []

    parts.append(build_page_frontmatter(page, raw_frames=raw_frames))
    parts.append("")

    # H1 header — normalize empty file/page names to (Unnamed) so the
    # rendered breadcrumb is never ``# /`` or similar.
    parts.append(f"# {normalize_name(page.file_name)} / {normalize_name(page.page_name)}")
    parts.append("")

    # Figma URL
    parts.append(f"[Open in Figma]({page.figma_url})")
    parts.append("")

    # Page summary placeholder
    if page.page_summary:
        parts.append(page.page_summary)
    else:
        parts.append("<!-- LLM: Write a 2-3 sentence page summary describing what this page covers -->")
    parts.append("")

    # Per-section: optional intro + table.
    # Format primitives live in :mod:`figmaclaw.figma_schema`. This section
    # MUST NOT inline section/row string formatting — the schema module is
    # the only place that knows the exact byte-level shape, and it's
    # bijectively paired with the parser.
    screen_sections = [s for s in page.sections if not s.is_component_library]
    table_header, table_separator = render_frame_table_header()
    for section in screen_sections:
        parts.append(render_section_heading(section.name, section.node_id))
        parts.append("")
        if section.intro:
            parts.append(section.intro)
        else:
            parts.append("<!-- LLM: Write a 1-sentence section intro if the section has a distinct theme -->")
        parts.append("")
        parts.append(table_header)
        parts.append(table_separator)
        for frame in section.frames:
            desc = frame.description if frame.description else PLACEHOLDER_DESCRIPTION
            parts.append(render_frame_row(frame.name, frame.node_id, desc))
        parts.append("")

    # Mermaid flowchart
    if page.flows:
        node_labels: dict[str, str] = {
            frame.node_id: normalize_name(frame.name)
            for section in screen_sections
            for frame in section.frames
        }
        parts.append(render_prose_heading(SCREEN_FLOW_SECTION))
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
    else:
        parts.append("<!-- LLM: Generate Mermaid flowchart from the flows in frontmatter, or omit if no flows -->")
        parts.append("")

    return "\n".join(parts)


def render_component_section(
    section: FigmaSection,
    page: FigmaPage,
    *,
    component_set_keys: dict[str, str] | None = None,
) -> str:
    """Render a component library section to a standalone semantic markdown file.

    Output: figma/{file-slug}/components/{section-slug}.md
    Format: YAML frontmatter + title + variants table (no flows, no Mermaid).

    component_set_keys: maps component-set name → Figma key for this section,
    fetched by the pull pass from GET /v1/files/{file_key}/component_sets.
    Written to frontmatter so build skills can call importComponentSetByKeyAsync()
    without a runtime search_design_system() MCP call.
    """
    parts: list[str] = []

    frame_ids: list[str] = [f.node_id for f in section.frames]

    parts.append(_build_frontmatter(
        file_key=page.file_key,
        page_node_id=page.page_node_id,
        section_node_id=section.node_id,
        frame_ids=frame_ids,
        flows=[],
        component_set_keys=component_set_keys,
    ))
    parts.append("")

    # H1: file / page / section — normalized so empty names don't leak
    # through as bare slashes.
    parts.append(
        f"# {normalize_name(page.file_name)} / "
        f"{normalize_name(page.page_name)} / "
        f"{normalize_name(section.name)}"
    )
    parts.append("")

    # Deep link to the section node
    section_url = (
        f"https://www.figma.com/design/{page.file_key}"
        f"?node-id={section.node_id.replace(':', '-')}"
    )
    parts.append(f"[Open in Figma]({section_url})")
    parts.append("")

    # Variants table — uses the fixed "Variants" name for the section
    # heading regardless of the source section's name in Figma.
    parts.append(render_section_heading(VARIANTS_SECTION, section.node_id))
    parts.append("")
    variant_header, variant_separator = render_variant_table_header()
    parts.append(variant_header)
    parts.append(variant_separator)
    for frame in section.frames:
        desc = frame.description if frame.description else PLACEHOLDER_DESCRIPTION
        parts.append(render_frame_row(frame.name, frame.node_id, desc))
    parts.append("")

    return "\n".join(parts)


def _mermaid_id(node_id: str) -> str:
    """Convert a node ID like '11:1' to a safe Mermaid identifier."""
    return "n" + node_id.replace(":", "_")
