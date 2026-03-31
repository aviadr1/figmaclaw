"""Parse YAML frontmatter from figmaclaw-rendered markdown files.

Policy: all structured data for machines lives in the YAML frontmatter.
        The frontmatter schema is FigmaPageFrontmatter (Pydantic).

parse_frontmatter() is the primary entry point — it returns the full
FigmaPageFrontmatter model. The convenience functions parse_page_metadata()
and parse_frame_descriptions() delegate to it.
"""

from __future__ import annotations

import re

import yaml

from figmaclaw.figma_frontmatter import FigmaclawMeta, FigmaPageFrontmatter

_FRONTMATTER_RE = re.compile(r"^---\n(.+?)\n---", re.DOTALL)


def parse_frontmatter(md: str) -> FigmaPageFrontmatter | None:
    """Parse and validate the YAML frontmatter block from a rendered page.

    Returns None if no frontmatter is found or it doesn't have a 'figmaclaw' key.
    """
    match = _FRONTMATTER_RE.match(md)
    if not match:
        return None
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict) or "figmaclaw" not in data:
        return None
    return FigmaPageFrontmatter.model_validate(data)


def parse_page_metadata(md: str) -> FigmaclawMeta | None:
    """Extract file_key, page_node_id, page_hash from the frontmatter.

    Returns None if no figmaclaw frontmatter is found.
    """
    fm = parse_frontmatter(md)
    return fm.figmaclaw if fm else None


def parse_frame_descriptions(md: str) -> dict[str, str]:
    """Extract {frame_name: description} from the frontmatter.

    Returns empty dict if the file has no figmaclaw frontmatter.
    """
    fm = parse_frontmatter(md)
    return fm.frames if fm else {}
