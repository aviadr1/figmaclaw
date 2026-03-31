"""Parse machine-readable metadata from figmaclaw-rendered markdown files.

Used for incremental pulls — read back what was previously written so we can
preserve existing descriptions for unchanged frames and detect page hashes.
"""

from __future__ import annotations

import re

from pydantic import BaseModel


class PageMetadata(BaseModel):
    """Structured metadata extracted from the HTML comment in a rendered page."""

    file_key: str
    page_node_id: str
    page_hash: str


_COMMENT_RE = re.compile(r"<!--\s*figmaclaw:\s*(.+?)\s*-->", re.DOTALL)
_KV_RE = re.compile(r"(\w+)=(\S+)")

# Matches 3-column section table rows: | frame name | `node_id` | description |
# Does NOT match 4-column Quick Reference rows: | frame | `id` | section | desc |
_TABLE_ROW_RE = re.compile(r"^\|\s*(.+?)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|$")


def parse_page_metadata(md: str) -> PageMetadata | None:
    """Extract file_key, page_node_id, page_hash from the HTML comment.

    Returns None if no figmaclaw comment is found.
    """
    match = _COMMENT_RE.search(md)
    if not match:
        return None

    body = match.group(1)
    kv: dict[str, str] = {m.group(1): m.group(2) for m in _KV_RE.finditer(body)}

    try:
        return PageMetadata(
            file_key=kv["file_key"],
            page_node_id=kv["page_node_id"],
            page_hash=kv["page_hash"],
        )
    except KeyError:
        return None


def parse_frame_descriptions(md: str) -> dict[str, str]:
    """Extract {frame_name: description} from a rendered figmaclaw markdown file.

    Reads from section tables (3-column: Screen | Node ID | Description).
    Skips header rows and Quick Reference table (4-column).
    Returns empty dict if the file has no figmaclaw tables.
    """
    # Only parse files that have the figmaclaw comment
    if "<!-- figmaclaw:" not in md:
        return {}

    descriptions: dict[str, str] = {}
    for line in md.splitlines():
        m = _TABLE_ROW_RE.match(line)
        if m:
            frame_name = m.group(1)
            description = m.group(3)
            # Skip header rows
            if frame_name.lower() == "screen":
                continue
            # Skip separator rows
            if set(description.replace("-", "").replace("|", "").strip()) <= set():
                continue
            descriptions[frame_name] = description

    return descriptions
