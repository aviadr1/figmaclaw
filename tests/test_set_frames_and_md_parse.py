"""Tests for figma_md_parse.py.

INVARIANTS:
- figma_md_parse.parse_sections extracts section/frame structure from body
- figma_md_parse.parse_sections leaves description empty (body is source of truth)
- figma_md_parse.parse_sections is robust to any column header name
"""

from __future__ import annotations

import textwrap

import pytest

from figmaclaw.figma_md_parse import parse_sections, section_line_ranges
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_sync_state import PageEntry


def _make_page(sections=None, flows=None):
    return FigmaPage(
        file_key="hOV4QM",
        file_name="Web App",
        page_node_id="7741:45837",
        page_name="Test Page",
        page_slug="test-page",
        figma_url="https://www.figma.com/design/hOV4QM?node-id=7741-45837",
        sections=sections or [],
        flows=flows or [],
        version="1",
        last_modified="2026-03-31T12:00:00Z",
    )


def _make_entry():
    return PageEntry(
        page_name="Test Page",
        page_slug="test-page",
        md_path="figma/hOV4QM/pages/test-page.md",
        page_hash="deadbeef",
        last_refreshed_at="2026-03-31T12:00:00Z",
    )


def _rendered_md_with_frame(node_id: str, name: str, description: str = "") -> str:
    frame = FigmaFrame(node_id=node_id, name=name, description=description)
    section = FigmaSection(node_id="10:1", name="Onboarding", frames=[frame])
    return scaffold_page(_make_page(sections=[section]), _make_entry())


# --- parse_sections: structure extraction ---

def test_parse_sections_extracts_section_names_and_node_ids():
    """INVARIANT: Section names and node IDs are parsed from body H2 headers."""
    md = _rendered_md_with_frame("11:1", "Welcome")
    sections = parse_sections(md)
    assert len(sections) == 1
    assert sections[0].name == "Onboarding"
    assert sections[0].node_id == "10:1"


def test_parse_sections_extracts_frame_names_and_node_ids():
    """INVARIANT: Frame names and node IDs are parsed from body table rows."""
    frame_a = FigmaFrame(node_id="11:1", name="Welcome screen", description="desc A")
    frame_b = FigmaFrame(node_id="11:2", name="Login screen", description="desc B")
    section = FigmaSection(node_id="10:1", name="Auth", frames=[frame_a, frame_b])
    md = scaffold_page(_make_page(sections=[section]), _make_entry())

    sections = parse_sections(md)
    assert len(sections) == 1
    frames = sections[0].frames
    node_ids = [f.node_id for f in frames]
    names = [f.name for f in frames]
    assert "11:1" in node_ids
    assert "11:2" in node_ids
    assert "Welcome screen" in names
    assert "Login screen" in names


def test_parse_sections_exposes_no_description_frontmatter_is_source_of_truth():
    """INVARIANT: ParsedFrame has only name and node_id — no description field.

    Descriptions live in YAML frontmatter (parse_frontmatter), never in ParsedFrame.
    This prevents callers from accidentally reading stale body text instead of
    the authoritative frontmatter data.
    """
    frame = FigmaFrame(node_id="11:1", name="Frame", description="a real description")
    section = FigmaSection(node_id="10:1", name="Sect", frames=[frame])
    md = scaffold_page(_make_page(sections=[section]), _make_entry())

    sections = parse_sections(md)
    for s in sections:
        for f in s.frames:
            assert hasattr(f, "name")
            assert hasattr(f, "node_id")
            assert not hasattr(f, "description"), (
                "ParsedFrame must not expose a description attribute — "
                "use parse_frontmatter() to read frame descriptions"
            )


def test_parse_sections_skips_screen_flow():
    """INVARIANT: The Screen Flow section is not included in parse_sections output."""
    frame = FigmaFrame(node_id="11:1", name="Frame", description="")
    section = FigmaSection(node_id="10:1", name="Onboarding", frames=[frame])
    page = _make_page(sections=[section], flows=[("11:1", "11:2")])
    md = scaffold_page(page, _make_entry())

    sections = parse_sections(md)
    names = [s.name for s in sections]
    assert "Screen Flow" not in names


def test_parse_sections_robust_to_any_column_header_name():
    """INVARIANT: parse_sections works even if the header row uses a different column name.

    Previously '| Screen ' and '| Variant ' were hardcoded triggers. Now the
    table is detected by the '|---' separator, so any column name works.
    """
    md = textwrap.dedent("""\
        ---
        file_key: abc
        page_node_id: '1:1'
        frames: {}
        ---

        ## My Section (`10:1`)

        | Renamed Column | Node ID | Description |
        |---|---|---|
        | Frame Name | `11:1` | some text |

    """)
    sections = parse_sections(md)
    assert len(sections) == 1
    assert len(sections[0].frames) == 1
    assert sections[0].frames[0].node_id == "11:1"
    assert sections[0].frames[0].name == "Frame Name"


# --- section_line_ranges ---


_MULTI_SECTION_MD = """\
---
file_key: abc
page_node_id: '1:1'
---

# File / Page

[Open in Figma](https://figma.com)

Page summary text.

## Auth (`10:1`)

Auth section intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | login desc |
| Signup | `11:2` | signup desc |

## Dashboard (`20:1`)

Dashboard intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Home | `21:1` | home desc |

## Screen flows

```mermaid
flowchart LR
    A["Login"] --> B["Home"]
```
"""


def test_section_line_ranges_returns_all_sections():
    """section_line_ranges returns one entry per ## heading, including Screen flows."""
    ranges = section_line_ranges(_MULTI_SECTION_MD)
    names = [s.name for s, _, _ in ranges]
    assert "Auth" in names
    assert "Dashboard" in names
    assert "Screen flows" in names
    assert len(ranges) == 3


def test_section_line_ranges_boundaries():
    """start_line is the ## heading, end_line is the next ## heading."""
    ranges = section_line_ranges(_MULTI_SECTION_MD)
    lines = _MULTI_SECTION_MD.splitlines()

    auth_section, auth_start, auth_end = ranges[0]
    assert auth_section.name == "Auth"
    assert lines[auth_start].startswith("## Auth")
    assert lines[auth_end].startswith("## Dashboard")

    dash_section, dash_start, dash_end = ranges[1]
    assert dash_section.name == "Dashboard"
    assert lines[dash_start].startswith("## Dashboard")
    assert lines[dash_end].startswith("## Screen flows")


def test_section_line_ranges_last_section():
    """Last section's end_line is len(lines)."""
    ranges = section_line_ranges(_MULTI_SECTION_MD)
    _, _, last_end = ranges[-1]
    assert last_end == len(_MULTI_SECTION_MD.splitlines())


def test_section_line_ranges_includes_frames():
    """Sections have their frames parsed from table rows."""
    ranges = section_line_ranges(_MULTI_SECTION_MD)
    auth_section = ranges[0][0]
    assert len(auth_section.frames) == 2
    assert auth_section.frames[0].node_id == "11:1"

    dash_section = ranges[1][0]
    assert len(dash_section.frames) == 1
    assert dash_section.frames[0].node_id == "21:1"

    # Screen flows has no frame table
    flows_section = ranges[2][0]
    assert len(flows_section.frames) == 0


def test_section_line_ranges_screen_flows_has_empty_node_id():
    """Screen flows heading has no (node_id) — its node_id should be empty."""
    ranges = section_line_ranges(_MULTI_SECTION_MD)
    flows = [s for s, _, _ in ranges if s.name == "Screen flows"]
    assert len(flows) == 1
    assert flows[0].node_id == ""


def test_parse_sections_still_skips_screen_flows():
    """parse_sections (which delegates to section_line_ranges) still skips Screen flows."""
    sections = parse_sections(_MULTI_SECTION_MD)
    names = [s.name for s in sections]
    assert "Screen flows" not in names
    assert len(sections) == 2
