"""Parse YAML frontmatter from figmaclaw-rendered markdown files.

Policy: all structured data for machines lives in the YAML frontmatter.
        The frontmatter schema is FigmaPageFrontmatter (Pydantic).

parse_frontmatter() is the primary entry point — it returns the full
FigmaPageFrontmatter model. The convenience functions delegate to it.

Note: frames are keyed by node_id (not frame name) so duplicate frame names
across sections never collide.

Backward compatibility: old files have a nested `figmaclaw:` block. The parser
detects this and promotes the nested fields to the flat top-level schema.
"""

from __future__ import annotations

import re

import yaml

from figmaclaw.figma_frontmatter import FigmaPageFrontmatter

_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---", re.DOTALL)


def parse_frontmatter(md: str) -> FigmaPageFrontmatter | None:
    """Parse and validate the YAML frontmatter block from a rendered page.

    Returns None if no frontmatter is found or it doesn't look like a figmaclaw file.
    Handles both the current flat schema and the legacy nested `figmaclaw:` schema.
    """
    match = _FRONTMATTER_RE.match(md)
    if not match:
        return None
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        return None

    # New flat schema: has top-level file_key
    if "file_key" in data:
        return FigmaPageFrontmatter.model_validate(data)

    # Legacy nested schema: has figmaclaw: {file_key, page_node_id, page_hash, section_node_id}
    if "figmaclaw" in data:
        meta = data.get("figmaclaw") or {}
        flat = {
            "file_key": meta.get("file_key", ""),
            "page_node_id": str(meta.get("page_node_id", "")),
            "section_node_id": meta.get("section_node_id"),
            "frames": data.get("frames", {}),
            "flows": data.get("flows", []),
        }
        return FigmaPageFrontmatter.model_validate(flat)

    return None


def parse_page_metadata(md: str) -> FigmaPageFrontmatter | None:
    """Extract file_key and page_node_id from the frontmatter.

    Returns None if no figmaclaw frontmatter is found.
    Callers access .file_key and .page_node_id directly on the returned object.
    """
    return parse_frontmatter(md)


def parse_frame_descriptions(md: str) -> dict[str, str]:
    """Extract {node_id: description} from the frontmatter.

    Returns empty dict if the file has no figmaclaw frontmatter.
    Keys are node IDs (e.g. "10635:89503"), not frame names.
    """
    fm = parse_frontmatter(md)
    return fm.frames if fm else {}


def parse_flows(md: str) -> list[tuple[str, str]]:
    """Extract flow edges from the frontmatter as [(src_node_id, dst_node_id), ...].

    Returns empty list if the file has no figmaclaw frontmatter or no flows.
    """
    fm = parse_frontmatter(md)
    if not fm or not fm.flows:
        return []
    return [(edge[0], edge[1]) for edge in fm.flows if len(edge) == 2]
