"""Parse the human-readable body of a figmaclaw-rendered markdown file.

This module is a **thin facade** over :mod:`figmaclaw.figma_schema`, which
holds the canonical render/parse primitives. It preserves the
``section_line_ranges`` / ``parse_sections`` / ``ParsedSection`` /
``ParsedFrame`` API for backward compatibility with existing callers
(``inspect``, ``claude_run``, ``screenshots``, ``write_body``).

New code should import directly from :mod:`figmaclaw.figma_schema`.

Policy reminder (unchanged): structured data lives in the YAML frontmatter
(``figma_parse.py``). This module parses the body for section structure and
frame node ids only — **never** prose (page summary, section intros,
Mermaid). Frame descriptions come from frontmatter, not from the body.

The Screen Flow section has no node_id in its heading and is excluded by
:func:`parse_sections` but included as a boundary by
:func:`section_line_ranges`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from figmaclaw.figma_schema import (
    is_h2,
    is_table_separator,
    parse_frame_row,
    parse_section_heading,
)


@dataclass
class ParsedFrame:
    """A frame parsed from a markdown table row.

    Exposes ``name`` and ``node_id`` only. Descriptions come from the YAML
    frontmatter (``figmaclaw.figma_parse.parse_frontmatter``), never from
    the body.
    """

    name: str
    node_id: str


@dataclass
class ParsedSection:
    """A section parsed from the markdown body.

    Frame sections have a non-empty ``node_id`` (extracted from the
    ``(`...`)`` suffix of their heading). Prose sections like
    ``## Screen Flow`` have ``node_id == ""``.
    """

    name: str
    node_id: str
    frames: list[ParsedFrame] = field(default_factory=list)


def section_line_ranges(md: str) -> list[tuple[ParsedSection, int, int]]:
    """Return ``(section, start_line, end_line)`` for every ``## `` heading.

    *start_line* is the index of the heading line (inclusive). *end_line* is
    the index of the next ``## `` heading or ``len(lines)`` (exclusive).

    The **Screen Flow** / ``## Screen flows`` section IS included — callers
    that want to skip prose sections can check ``section.node_id``.

    Used by ``write-body --section`` to surgically replace one section and
    by ``inspect`` to count pending/stale frames per section.
    """
    lines = md.splitlines()

    # First pass: find every H2 boundary.
    headings: list[tuple[int, ParsedSection]] = []
    for i, line in enumerate(lines):
        if not is_h2(line):
            continue
        parsed = parse_section_heading(line)
        if parsed is None:
            # is_h2 matched but parse_section_heading returned None —
            # means the line starts with ``## `` but matches no known form.
            # Treat as a boundary with empty name/id so callers can still
            # slice around it.
            headings.append((i, ParsedSection(name="", node_id="")))
            continue
        headings.append(
            (i, ParsedSection(name=parsed.name, node_id=parsed.node_id))
        )

    if not headings:
        return []

    # Second pass: extract frame rows inside each section's line range.
    result: list[tuple[ParsedSection, int, int]] = []
    for idx, (start, section) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        in_table = False
        for line in lines[start:end]:
            if is_table_separator(line):
                in_table = True
                continue
            if in_table:
                row = parse_frame_row(line)
                if row is not None:
                    section.frames.append(
                        ParsedFrame(name=row.name, node_id=row.node_id)
                    )
                elif not line.strip():
                    in_table = False
        result.append((section, start, end))

    return result


def parse_sections(md: str) -> list[ParsedSection]:
    """Return frame sections from *md* in document order.

    Prose sections (``## Screen Flow`` and any other H2 without a
    ``(`node_id`)`` suffix) are skipped. Component library files with a
    ``Variants`` section are handled identically to page files.
    """
    return [
        section
        for section, _, _ in section_line_ranges(md)
        if section.node_id
    ]
