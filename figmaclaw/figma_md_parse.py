"""Parse the human-readable body of a figmaclaw-rendered markdown file.

Policy: structured data lives in the YAML frontmatter (figma_parse.py handles that).
This module parses the *body* — section headings and frame table rows — so that
agents can inspect page structure without calling the Figma API.

Parsing strategy: line-by-line scan, no regex soup.
  - Section headers: `## <name> (`<node_id>`)`
  - Table rows:      `| <name> | `<node_id>` | ... |`  (columns after node_id are ignored)
  - Separator rows and header rows are skipped.

Frame descriptions are NOT extracted from the body — read them from YAML frontmatter
via figma_parse.parse_frontmatter() which is the source of truth.

NEVER parse prose from the body (page summary, section intros, Mermaid). Prose is
read and written by humans and LLMs only — not by code.

The Screen Flow section (## Screen Flow) is skipped — it contains a Mermaid diagram,
not a frame table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field  # field used by ParsedSection

_SECTION_RE = re.compile(r"^## (.+?) \(`([^`]+)`\)\s*$")
_ANY_H2_RE = re.compile(r"^## ")
# Match any table row that has a backtick-quoted node_id in the second column.
# We only capture name and node_id; descriptions come from YAML frontmatter (source of truth).
_FRAME_ROW_RE = re.compile(r"^\| ([^|]+) \| `([^`]+)` \|")
_SKIP_SECTIONS = {"Screen Flow"}


@dataclass
class ParsedFrame:
    name: str
    node_id: str


@dataclass
class ParsedSection:
    name: str
    node_id: str
    frames: list[ParsedFrame] = field(default_factory=list)


def section_line_ranges(md: str) -> list[tuple[ParsedSection, int, int]]:
    """Return ``(section, start_line, end_line)`` for each ``## `` section.

    *start_line* is the index of the ``## `` heading line (inclusive).
    *end_line* is the index of the next ``## `` heading or ``len(lines)``
    (exclusive).

    The **Screen flows** section (Mermaid diagram) IS included — callers
    that need it for boundary detection can check ``section.name``.

    Used by ``write-body --section`` to surgically replace one section, and
    by ``inspect`` to count pending/stale frames per section.
    """
    lines = md.splitlines()
    # First pass: find all ## heading positions and parse them
    headings: list[tuple[int, ParsedSection | None]] = []
    for i, line in enumerate(lines):
        if _ANY_H2_RE.match(line):
            m = _SECTION_RE.match(line)
            if m:
                headings.append((i, ParsedSection(name=m.group(1), node_id=m.group(2))))
            else:
                # Non-section ## heading (e.g. "## Screen flows") — still a boundary
                raw_name = line.lstrip("# ").strip()
                headings.append((i, ParsedSection(name=raw_name, node_id="")))

    if not headings:
        return []

    # Second pass: extract frames within each section's line range
    result: list[tuple[ParsedSection, int, int]] = []
    for idx, (start, section) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        assert section is not None
        # Parse frame rows within this range
        in_table = False
        for line in lines[start:end]:
            if line.startswith("|---") or line.startswith("| ---"):
                in_table = True
                continue
            if in_table and line.startswith("|"):
                m2 = _FRAME_ROW_RE.match(line)
                if m2:
                    section.frames.append(ParsedFrame(
                        name=m2.group(1).strip(),
                        node_id=m2.group(2).strip(),
                    ))
            elif in_table and not line.strip():
                in_table = False
        result.append((section, start, end))

    return result


def parse_sections(md: str) -> list[ParsedSection]:
    """Extract sections and their frames from a figmaclaw page .md body.

    Returns sections in document order, skipping Screen Flow (Mermaid diagram section).
    Component library files (with a 'Variants' section) are handled identically.
    """
    return [
        section
        for section, _, _ in section_line_ranges(md)
        if section.name not in _SKIP_SECTIONS and section.node_id
    ]
