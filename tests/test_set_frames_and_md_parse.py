"""Tests for set_frames.py and figma_md_parse.py.

INVARIANTS:
- set-frames round-trips descriptions containing pipe characters without corruption
- set-frames updates both frontmatter and body table for standard descriptions
- figma_md_parse.parse_sections extracts section/frame structure from body
- figma_md_parse.parse_sections leaves description empty (frontmatter is source of truth)
- figma_md_parse.parse_sections is robust to any column header name
- page-tree uses frontmatter for description/missing counts, not body text
"""

from __future__ import annotations

import textwrap

import pytest

from figmaclaw.commands.set_frames import _apply_descriptions, _apply_frontmatter
from figmaclaw.figma_md_parse import parse_sections
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import render_page
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
    return render_page(_make_page(sections=[section]), _make_entry())


# --- _apply_descriptions: pipe characters in description ---

def test_apply_descriptions_plain_description_updates_body():
    """INVARIANT: A plain description (no pipes) is written into the correct table cell."""
    md = _rendered_md_with_frame("11:1", "Welcome screen")
    result = _apply_descriptions(md, {"11:1": "User lands here after install."})
    assert "| Welcome screen | `11:1` | User lands here after install. |" in result


def test_apply_descriptions_pipe_in_description_round_trips():
    """INVARIANT: A description containing '|' is written and read back intact.

    Previously the greedy (.*| ) group would consume part of the description,
    leaving only the fragment after the last '|' in the cell.
    """
    md = _rendered_md_with_frame("11:1", "Welcome screen")
    desc = "primary | secondary layout"
    result = _apply_descriptions(md, {"11:1": desc})
    assert f"| Welcome screen | `11:1` | {desc} |" in result


def test_apply_descriptions_multiple_pipes_in_description():
    """INVARIANT: Multiple pipes in a description all survive the update."""
    md = _rendered_md_with_frame("11:1", "Picker")
    desc = "a | b | c | d"
    result = _apply_descriptions(md, {"11:1": desc})
    assert f"| Picker | `11:1` | {desc} |" in result


def test_apply_descriptions_does_not_alter_other_rows():
    """INVARIANT: Rows for node IDs not in the update dict are left unchanged."""
    frame_a = FigmaFrame(node_id="11:1", name="Frame A", description="")
    frame_b = FigmaFrame(node_id="11:2", name="Frame B", description="existing desc")
    section = FigmaSection(node_id="10:1", name="Sect", frames=[frame_a, frame_b])
    md = render_page(_make_page(sections=[section]), _make_entry())

    result = _apply_descriptions(md, {"11:1": "New for A"})
    assert "| Frame A | `11:1` | New for A |" in result
    assert "| Frame B | `11:2` | existing desc |" in result


def test_apply_frontmatter_persists_pipe_description():
    """INVARIANT: A description with pipes is stored verbatim in the YAML frontmatter."""
    md = _rendered_md_with_frame("11:1", "Frame")
    desc = "left | right split"
    updated = _apply_frontmatter(md, {"11:1": desc}, flows=None)
    fm = parse_frontmatter(updated)
    assert fm is not None
    assert fm.frames["11:1"] == desc


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
    md = render_page(_make_page(sections=[section]), _make_entry())

    sections = parse_sections(md)
    assert len(sections) == 1
    frames = sections[0].frames
    node_ids = [f.node_id for f in frames]
    names = [f.name for f in frames]
    assert "11:1" in node_ids
    assert "11:2" in node_ids
    assert "Welcome screen" in names
    assert "Login screen" in names


def test_parse_sections_description_is_empty_frontmatter_is_source_of_truth():
    """INVARIANT: parse_sections always returns empty descriptions — callers use frontmatter."""
    frame = FigmaFrame(node_id="11:1", name="Frame", description="a real description")
    section = FigmaSection(node_id="10:1", name="Sect", frames=[frame])
    md = render_page(_make_page(sections=[section]), _make_entry())

    sections = parse_sections(md)
    assert all(f.description == "" for s in sections for f in s.frames)


def test_parse_sections_skips_quick_reference():
    """INVARIANT: The Quick Reference section is not included in parse_sections output."""
    frame = FigmaFrame(node_id="11:1", name="Frame", description="")
    section = FigmaSection(node_id="10:1", name="Onboarding", frames=[frame])
    md = render_page(_make_page(sections=[section]), _make_entry())

    sections = parse_sections(md)
    names = [s.name for s in sections]
    assert "Quick Reference" not in names


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
