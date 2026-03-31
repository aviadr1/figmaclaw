"""Parse the human-readable body of a figmaclaw-rendered markdown file.

Policy: structured data lives in the YAML frontmatter (figma_parse.py handles that).
This module parses the *body* — section headings and frame table rows — so that
agents can inspect page structure without calling the Figma API.

Parsing strategy: line-by-line scan, no regex soup.
  - Section headers: `## <name> (`<node_id>`)`
  - Table rows:      `| <name> | `<node_id>` | <description> |`
  - Separator rows and header rows are skipped.

The Quick Reference section (## Quick Reference) is intentionally skipped —
it duplicates the per-section tables and would inflate the output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SECTION_RE = re.compile(r"^## (.+?) \(`([^`]+)`\)\s*$")
_ANY_H2_RE = re.compile(r"^## ")
# 3-column row only: | name | `node_id` | description |
# We reject 4-column rows (Quick Reference has a Section column) by anchoring to end-of-line.
_FRAME_ROW_RE = re.compile(r"^\| ([^|]*) \| `([^`]+)` \| ([^|]*) \|\s*$")
_PLACEHOLDER = "(no description yet)"
_SKIP_SECTIONS = {"Quick Reference", "Screen Flow"}


@dataclass
class ParsedFrame:
    name: str
    node_id: str
    description: str  # empty string when placeholder

    @property
    def needs_description(self) -> bool:
        return not self.description


@dataclass
class ParsedSection:
    name: str
    node_id: str
    frames: list[ParsedFrame] = field(default_factory=list)


def parse_sections(md: str) -> list[ParsedSection]:
    """Extract sections and their frames from a figmaclaw page .md body.

    Returns sections in document order, skipping Quick Reference and Screen Flow.
    Component library files (with a 'Variants' section) are handled identically.
    """
    sections: list[ParsedSection] = []
    current: ParsedSection | None = None
    in_table = False

    for line in md.splitlines():
        # Any ## heading resets the active section first
        if _ANY_H2_RE.match(line):
            current = None
            in_table = False
            m = _SECTION_RE.match(line)
            if m:
                name, node_id = m.group(1), m.group(2)
                if name not in _SKIP_SECTIONS:
                    current = ParsedSection(name=name, node_id=node_id)
                    sections.append(current)
            continue

        if current is None:
            continue

        # Table header / separator
        if line.startswith("| Screen ") or line.startswith("| Variant ") or line.startswith("|---") or line.startswith("|----"):
            in_table = True
            continue

        if in_table and line.startswith("|"):
            m2 = _FRAME_ROW_RE.match(line)
            if m2:
                name_cell = m2.group(1).strip()
                node_id_cell = m2.group(2).strip()
                desc_cell = m2.group(3).strip()
                description = "" if desc_cell == _PLACEHOLDER else desc_cell
                current.frames.append(ParsedFrame(
                    name=name_cell,
                    node_id=node_id_cell,
                    description=description,
                ))
        elif in_table and not line.strip():
            in_table = False

    return sections
