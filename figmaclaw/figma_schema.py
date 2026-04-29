"""Canonical schema for figmaclaw's Figma → markdown → Figma pipeline.

This is the **single source of truth** for:

1. **Ingestion predicates** — which Figma API nodes produce rendered content,
   which are visible, which are structural vs component, how raw names are
   normalized.
2. **Rendering primitives** — the exact byte-level format for section
   headings, frame table rows, and table headers. Every file writer MUST
   call these functions rather than inlining f-strings.
3. **Parsing primitives** — the inverse of the rendering primitives. Every
   file reader MUST call these functions rather than inlining regexes.
4. **Round-trip invariant** — for any valid ``(name, node_id)`` pair,
   ``parse_section_heading(render_section_heading(name, node_id))`` yields
   the canonical form of the input.

Why this exists
---------------
Before this module, schema knowledge was duplicated across at least six
files (``figma_models``, ``figma_render``, ``figma_md_parse``, ``figma_hash``,
``commands/write_body``, ``commands/write_descriptions``, ``commands/claude_run``).
The duplication drifted — notably, ``figma_render`` happily wrote section
headings with empty names (``##  (`id`)``) that ``figma_md_parse`` could
not read back, causing every frame under such a section to be silently
dropped from enrichment. See figmaclaw#25 for the original bug.

Design rules
------------
* **Pure data transforms.** No I/O, no subprocess, no network.
* **No pydantic.** Dependency-light; importable from anywhere without
  pulling in the model layer.
* **Normalization is idempotent.** ``normalize_name(normalize_name(x))
  == normalize_name(x)``.
* **``render_*`` is injective on normalized input; ``parse_*`` is its
  inverse.** Tested explicitly in ``tests/test_figma_schema.py``.
* **Functions that reject invalid input raise ``ValueError``** with a
  specific message — never return ``None`` silently (except parse
  functions whose contract is "return None if input isn't this shape").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants — the exact strings that appear in rendered markdown.
# ---------------------------------------------------------------------------

#: Description placeholder used for frames that haven't been described yet.
#: Referenced by claude_run, write_descriptions, screenshots, inspect, and the
#: enrichment prompts. Changing this requires updating the prompts too.
PLACEHOLDER_DESCRIPTION: str = "(no description yet)"

#: Canonical marker used when screenshot export failed for a frame.
NO_SCREENSHOT_AVAILABLE: str = "(no screenshot available)"

#: Legacy/alternate marker still seen in historical files and prompts.
SCREENSHOT_UNAVAILABLE: str = "(screenshot unavailable)"

#: Description-cell markers that mean the row is still unresolved/retryable.
UNRESOLVED_DESCRIPTION_MARKERS: frozenset[str] = frozenset(
    {
        PLACEHOLDER_DESCRIPTION,
        NO_SCREENSHOT_AVAILABLE,
        SCREENSHOT_UNAVAILABLE,
    }
)

#: Substitute for frame or section names that are empty / whitespace-only in
#: the source Figma data. Making this visible in the rendered markdown is
#: deliberate — a bare ``##  (`id`)`` heading is both ugly and historically
#: caused parser failures (figmaclaw#25).
UNNAMED: str = "(Unnamed)"

#: Synthetic section name used when top-level FRAME nodes are not grouped by
#: a SECTION parent in Figma.
UNGROUPED_SECTION: str = "(Ungrouped)"

#: Synthetic node_id used for the (Ungrouped) section. This does NOT
#: correspond to a real Figma node — downstream consumers (screenshots,
#: write-descriptions) must skip it when making API calls.
UNGROUPED_NODE_ID: str = "ungrouped"

#: Synthetic section name used when top-level COMPONENT/COMPONENT_SET nodes
#: are not grouped by a SECTION parent in Figma. Pages with components placed
#: directly on the canvas (no SECTION wrapper) used to drop silently — no
#: section, no .md file, page hash collapsing to the empty-list digest. This
#: synthetic section keeps them visible to pull and to the manifest. Marked
#: ``is_component_library=True`` so downstream rendering writes a component
#: .md alongside the page rather than a screen .md.
UNGROUPED_COMPONENTS_SECTION: str = "(Ungrouped components)"

#: Synthetic node_id for the ``(Ungrouped components)`` section. As with
#: ``UNGROUPED_NODE_ID``, no real Figma node corresponds to this id.
UNGROUPED_COMPONENTS_NODE_ID: str = "ungrouped-components"

#: Section name used by component library files (``figma/*/components/*.md``).
VARIANTS_SECTION: str = "Variants"

#: Heading used for the Mermaid flowchart. Has no node_id in its heading
#: (``## Screen Flow`` with no parenthesized id).
SCREEN_FLOW_SECTION: str = "Screen Flow"

#: Section names that never contain frame tables even if they happen to have
#: adjacent table-like lines. Used by downstream filters.
PROSE_SECTION_NAMES: frozenset[str] = frozenset({SCREEN_FLOW_SECTION, "Screen flows"})

# Table header and separator lines for frame tables. Written verbatim by
# :func:`render_frame_table_header`; the parser detects tables by the
# ``|---`` separator so column labels can drift without breaking reads.
_FRAME_TABLE_HEADER: str = "| Screen | Node ID | Description |"
_FRAME_TABLE_SEPARATOR: str = "|--------|---------|-------------|"
_VARIANT_TABLE_HEADER: str = "| Variant | Node ID | Description |"
_VARIANT_TABLE_SEPARATOR: str = "|---------|---------|-------------|"


# ---------------------------------------------------------------------------
# Figma node type classification.
# ---------------------------------------------------------------------------

#: Nodes that define page structure — contain frames or are themselves frames.
STRUCTURAL_NODE_TYPES: frozenset[str] = frozenset({"FRAME", "SECTION"})

#: Component library node types — rendered as variant tables, not frame tables.
COMPONENT_NODE_TYPES: frozenset[str] = frozenset({"COMPONENT", "COMPONENT_SET"})

#: Node types that become rendered rows in markdown. FRAMEs appear in page
#: files, COMPONENTs and COMPONENT_SETs appear in component library files.
RENDERABLE_NODE_TYPES: frozenset[str] = STRUCTURAL_NODE_TYPES | COMPONENT_NODE_TYPES


# ---------------------------------------------------------------------------
# Ingestion predicates (raw Figma API dict → bool).
# ---------------------------------------------------------------------------


def is_visible(node: dict) -> bool:
    """Return True if *node* should be rendered to markdown.

    Figma's ``visible`` field is optional and defaults to True. A node is
    visible unless the field is explicitly set to False. ``None`` and missing
    values are treated as visible — matches the semantics in every existing
    ``c.get("visible", True) is not False`` call site prior to consolidation.
    """
    return node.get("visible", True) is not False


def is_structural(node: dict) -> bool:
    """True if *node* is a FRAME or SECTION."""
    return node.get("type", "") in STRUCTURAL_NODE_TYPES


def is_component(node: dict) -> bool:
    """True if *node* is a COMPONENT or COMPONENT_SET."""
    return node.get("type", "") in COMPONENT_NODE_TYPES


def is_renderable_child(node: dict) -> bool:
    """True if *node* should be rendered as a row under a parent section.

    Combines the type filter (structural or component) with the visibility
    filter. This is the **one** predicate ingestion code should use to decide
    whether a child node becomes part of the rendered output.
    """
    return (is_structural(node) or is_component(node)) and is_visible(node)


def raw_name(node: dict) -> str:
    """Return ``node["name"]`` or empty string — the raw, un-normalized form."""
    return node.get("name", "") or ""


# ---------------------------------------------------------------------------
# Name normalization.
# ---------------------------------------------------------------------------


def normalize_name(raw: str | None) -> str:
    """Normalize a Figma node name to a user-friendly, parseable form.

    Rules:

    * ``None`` → :data:`UNNAMED`.
    * Empty string → :data:`UNNAMED`.
    * Whitespace-only → :data:`UNNAMED`.
    * Otherwise → the input stripped of leading and trailing whitespace.

    Idempotent: ``normalize_name(normalize_name(x)) == normalize_name(x)``
    for every input.
    """
    if raw is None:
        return UNNAMED
    stripped = raw.strip()
    return stripped if stripped else UNNAMED


# ---------------------------------------------------------------------------
# Rendering primitives (model → markdown lines).
# ---------------------------------------------------------------------------


def render_section_heading(name: str | None, node_id: str) -> str:
    """Emit ``## <name> (`<node_id>`)`` for a frame / variant / ungrouped section.

    *name* is normalized via :func:`normalize_name`; empty or whitespace-only
    input becomes :data:`UNNAMED`. *node_id* must be non-empty; it's an
    opaque identifier from the Figma API (or the synthetic
    :data:`UNGROUPED_NODE_ID`).

    Raises :class:`ValueError` if *node_id* is empty.

    For prose-only sections (``## Screen Flow``), use
    :func:`render_prose_heading` instead.
    """
    if not node_id:
        raise ValueError("render_section_heading requires a non-empty node_id")
    return f"## {normalize_name(name)} (`{node_id}`)"


def render_prose_heading(name: str) -> str:
    """Emit ``## <name>`` for a section with no node_id (e.g. Screen Flow).

    *name* is stripped but not substituted — callers pass known constants
    like :data:`SCREEN_FLOW_SECTION`. Raises :class:`ValueError` on empty.
    """
    stripped = name.strip()
    if not stripped:
        raise ValueError("render_prose_heading requires a non-empty name")
    return f"## {stripped}"


def _escape_cell(text: str) -> str:
    """Escape pipe characters so they survive a markdown table cell."""
    return text.replace("|", "\\|")


def _unescape_cell(text: str) -> str:
    """Inverse of :func:`_escape_cell`."""
    return text.replace("\\|", "|")


def render_frame_row(name: str | None, node_id: str, description: str) -> str:
    """Emit ``| <name> | `<node_id>` | <desc> |`` — one frame table row.

    *name* is normalized (and then pipe-escaped for cell safety).
    *description* has pipes escaped the same way. *node_id* must be
    non-empty and is NOT escaped — Figma node ids have no pipes.

    The row is always round-trippable: :func:`parse_frame_row` will
    recover ``normalize_name(name)`` and ``node_id`` exactly, including
    when the name or description contains pipes.
    """
    if not node_id:
        raise ValueError("render_frame_row requires a non-empty node_id")
    safe_name = _escape_cell(normalize_name(name))
    safe_desc = _escape_cell(description or "")
    return f"| {safe_name} | `{node_id}` | {safe_desc} |"


def render_frame_table_header() -> tuple[str, str]:
    """Return ``(header_line, separator_line)`` for a page frame table."""
    return _FRAME_TABLE_HEADER, _FRAME_TABLE_SEPARATOR


def render_variant_table_header() -> tuple[str, str]:
    """Return ``(header_line, separator_line)`` for a component variants table."""
    return _VARIANT_TABLE_HEADER, _VARIANT_TABLE_SEPARATOR


# ---------------------------------------------------------------------------
# Parsing primitives (markdown line → structured).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionHeading:
    """A parsed H2 heading from a figmaclaw markdown file.

    * ``name``: normalized via :func:`normalize_name`. Always non-empty
      for frame sections; may be any value (including "") for prose
      headings where the name is used verbatim from the file.
    * ``node_id``: extracted from the ``(`...`)`` suffix. Empty string
      if the heading has no such suffix (prose-only sections like
      ``## Screen Flow``).
    """

    name: str
    node_id: str

    @property
    def is_frame_section(self) -> bool:
        """True if this heading identifies a section that can contain frames."""
        return bool(self.node_id)


@dataclass(frozen=True)
class FrameRow:
    """A parsed frame table row. ``name`` is normalized; ``node_id`` is raw."""

    name: str
    node_id: str


# Permissive section heading pattern:
#   ^##            — literal
#   \s*            — zero or more spaces after ``##``
#   (.*?)          — name (possibly empty; normalized by caller)
#   \s*            — zero or more spaces before the node_id parenthesis
#   \(`([^`]+)`\)  — ``(`node_id`)``
#   \s*$           — end of line, optional trailing whitespace
_SECTION_HEADING_RE = re.compile(r"^##\s*(.*?)\s*\(`([^`]+)`\)\s*$")

# Prose-only H2 — matches ``## Screen Flow`` etc. Requires the first
# character after ``##`` to be non-whitespace so we don't swallow malformed
# lines like ``##   (garbage``.
_PROSE_HEADING_RE = re.compile(r"^##\s+(\S.*?)\s*$")

# Any H2 heading — used as a boundary detector. Note the trailing space:
# we require ``## `` so ``##foo`` (malformed) is not treated as an H2.
_ANY_H2_RE = re.compile(r"^## ")

# Frame table row: ``| name | `node_id` | description |``
# Captures name (possibly empty, pipe-escaped) and node_id. Description
# is intentionally NOT captured — callers read descriptions from the YAML
# frontmatter.
#
# The name-cell pattern ``(?:\\\||[^|])*?`` matches either an escaped pipe
# (``\|``) or any non-pipe character. This lets frame names round-trip
# through the table when they contain pipes — rare in practice but
# supported for bijection correctness.
_FRAME_ROW_RE = re.compile(r"^\|\s*((?:\\\||[^|])*?)\s*\|\s*`([^`]+)`\s*\|")


def parse_section_heading(line: str) -> SectionHeading | None:
    """Parse an H2 line into a :class:`SectionHeading`, or return ``None``.

    Returns ``None`` when *line* isn't an H2 heading at all (doesn't start
    with ``##`` followed by a space).

    Returns a :class:`SectionHeading` with:

    * Non-empty ``node_id`` and normalized ``name`` for frame sections —
      e.g. ``## Onboarding (``10:1``)`` → ``name="Onboarding", node_id="10:1"``.
    * Empty ``node_id`` for prose-only headings — e.g. ``## Screen Flow``
      → ``name="Screen Flow", node_id=""``.

    **Round-trip guarantee**: for any heading produced by
    :func:`render_section_heading` or :func:`render_prose_heading`, this
    function returns a matching :class:`SectionHeading`.
    """
    if not _ANY_H2_RE.match(line):
        return None

    m = _SECTION_HEADING_RE.match(line)
    if m:
        return SectionHeading(name=normalize_name(m.group(1)), node_id=m.group(2))

    m = _PROSE_HEADING_RE.match(line)
    if m:
        return SectionHeading(name=m.group(1).strip(), node_id="")

    # ``## `` followed by only whitespace — an empty H2. Treat as a prose
    # heading with an empty name. Uncommon but legal markdown.
    return SectionHeading(name="", node_id="")


def parse_frame_row(line: str) -> FrameRow | None:
    """Parse a frame table row, or return ``None`` if *line* isn't one.

    Skips table separators (``|---|---|---|``). Returns a :class:`FrameRow`
    with a normalized name and raw node_id. Descriptions are intentionally
    NOT returned — callers must read them from the YAML frontmatter.

    Header rows (``| Screen | Node ID | Description |``) are naturally
    rejected because their second column is literal text, not a
    backtick-quoted node_id.
    """
    if is_table_separator(line):
        return None
    m = _FRAME_ROW_RE.match(line)
    if m is None:
        return None
    # Unescape pipes that were escaped by render_frame_row. We intentionally
    # call _unescape_cell BEFORE normalize_name so names like ``| pipe``
    # round-trip through normalization unchanged.
    return FrameRow(
        name=normalize_name(_unescape_cell(m.group(1))),
        node_id=m.group(2),
    )


def is_h2(line: str) -> bool:
    """True if *line* is an H2 heading of any form (``## ...``)."""
    return bool(_ANY_H2_RE.match(line))


def is_table_separator(line: str) -> bool:
    """True if *line* is a markdown table separator.

    Matches ``|---|---|---|``, ``| --- | --- | --- |``, ``|-----|-----|``,
    and similar. Detection is intentionally loose: any line whose first
    non-whitespace cell begins with ``-`` counts.
    """
    s = line.lstrip()
    if not s.startswith("|"):
        return False
    after_pipe = s[1:].lstrip()
    return after_pipe.startswith("-")


def is_placeholder_row(line: str) -> bool:
    """True if *line* still carries the canonical placeholder description."""
    return f"| {PLACEHOLDER_DESCRIPTION} |" in line


def is_unresolved_row(line: str) -> bool:
    """True if *line* has any unresolved/retryable description marker.

    Includes the canonical placeholder and screenshot-unavailable markers.
    """
    return any(f"| {marker} |" in line for marker in UNRESOLVED_DESCRIPTION_MARKERS)


# ---------------------------------------------------------------------------
# Round-trip invariants (exposed for tests and runtime assertions).
# ---------------------------------------------------------------------------


def unresolved_row_node_id(line: str) -> str | None:
    """Return node_id when *line* is an unresolved frame row, else None."""
    if not is_unresolved_row(line):
        return None
    row = parse_frame_row(line)
    return row.node_id if row is not None else None


def assert_section_round_trip(name: str | None, node_id: str) -> None:
    """Assert that render/parse is a bijection for ``(name, node_id)``.

    Raises :class:`AssertionError` with a diagnostic message if the
    invariant is broken. Safe to call in tests or as a runtime sanity
    check.
    """
    rendered = render_section_heading(name, node_id)
    parsed = parse_section_heading(rendered)
    expected_name = normalize_name(name)
    if parsed is None:
        raise AssertionError(
            f"round-trip failed: render_section_heading({name!r}, {node_id!r}) "
            f"→ {rendered!r} did not parse as an H2 heading"
        )
    if parsed.name != expected_name or parsed.node_id != node_id:
        raise AssertionError(
            f"round-trip mismatch for ({name!r}, {node_id!r}):\n"
            f"  rendered = {rendered!r}\n"
            f"  parsed   = {parsed!r}\n"
            f"  expected = SectionHeading(name={expected_name!r}, node_id={node_id!r})"
        )


def assert_frame_row_round_trip(name: str | None, node_id: str, description: str = "") -> None:
    """Assert the render/parse bijection for frame rows.

    Only ``(name, node_id)`` round-trip through the table format; the
    description is written but not read back (it comes from frontmatter).
    """
    rendered = render_frame_row(name, node_id, description)
    parsed = parse_frame_row(rendered)
    expected_name = normalize_name(name)
    if parsed is None:
        raise AssertionError(
            f"frame row round-trip failed: render_frame_row({name!r}, {node_id!r}) → {rendered!r}"
        )
    if parsed.name != expected_name or parsed.node_id != node_id:
        raise AssertionError(
            f"frame row round-trip mismatch for ({name!r}, {node_id!r}):\n"
            f"  rendered = {rendered!r}\n"
            f"  parsed   = {parsed!r}"
        )
