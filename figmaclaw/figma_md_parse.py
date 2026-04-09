"""Multi-line document parser for figmaclaw-rendered markdown files.

This module is the **document-level parser**: it walks a whole markdown
body and assembles higher-level structures (``ParsedSection`` with its
contained frames, line ranges for surgical edits) from the single-line
primitives provided by :mod:`figmaclaw.figma_schema`.

Two-layer design:

=====================  ==================================================
Layer                  Responsibility
=====================  ==================================================
``figma_schema``       Parse / render **one** line at a time. Regex
                       patterns, format constants, escape rules, single-
                       item dataclasses (``SectionHeading``, ``FrameRow``).

``figma_md_parse``     Walk a whole body, detect H2 section boundaries,
                       attach frame rows to their enclosing section,
                       return line ranges that downstream code uses for
                       surgical ``write-body --section`` edits.
=====================  ==================================================

Keep these responsibilities separate. Concretely, **do not add any of
the following to this module**:

* ``re`` imports or :func:`re.compile` calls — regex primitives live in
  :mod:`figma_schema`.
* String literals for ``## ``, ``| ... | `` ... ` | ... |``, ``(Unnamed)``,
  ``(Ungrouped)``, ``Screen Flow``, or ``(no description yet)`` — those
  are canonical constants in :mod:`figma_schema`. Import them.
* Visibility or node-type predicates — use
  :func:`figma_schema.is_visible`, :func:`figma_schema.is_structural`,
  etc.

If you need a new line-level primitive (a predicate, a renderer, a
format string), add it to :mod:`figma_schema` first and import it here.
There is a CI guardrail test in ``tests/test_figma_md_parse_guardrails.py``
that enforces these rules.

Policy reminders
----------------
* Structured data (frontmatter) is owned by :mod:`figmaclaw.figma_parse`,
  not this module. This module parses the **body** only.
* This module reads section structure and frame node ids from the body.
  It never parses prose — no page summary, no section intros, no Mermaid.
  Frame descriptions come from the YAML frontmatter, not the table cells.
* The ``## Screen Flow`` section has no node_id in its heading. It is
  included by :func:`section_line_ranges` as a boundary (so callers can
  slice around it) but excluded by :func:`parse_sections`.
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
        headings.append((i, ParsedSection(name=parsed.name, node_id=parsed.node_id)))

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
                    section.frames.append(ParsedFrame(name=row.name, node_id=row.node_id))
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
    return [section for section, _, _ in section_line_ranges(md) if section.node_id]
