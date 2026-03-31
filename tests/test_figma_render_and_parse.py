"""Tests for figma_render.py and figma_parse.py.

INVARIANTS:
- render_page produces YAML frontmatter with FigmaPageFrontmatter schema
- Frontmatter carries file_key, page_node_id, page_hash, and frame descriptions
- Body has H1 header, Figma URL, section tables, optional Mermaid, Quick Reference
- Section tables list all frames with node IDs
- Mermaid block absent when no flow edges
- Placeholder description used in table when frame has no description
- Frame descriptions in frontmatter are omitted when empty (no placeholder stored)
- figma_parse can recover metadata and frame descriptions from rendered output
"""

from __future__ import annotations

import pytest
import yaml

from figmaclaw.figma_frontmatter import FigmaPageFrontmatter
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import render_page
from figmaclaw.figma_parse import parse_frame_descriptions, parse_frontmatter, parse_page_metadata
from figmaclaw.figma_sync_state import PageEntry


def _make_page(
    sections: list[FigmaSection] | None = None,
    flows: list[tuple[str, str]] | None = None,
) -> FigmaPage:
    return FigmaPage(
        file_key="hOV4QM",
        file_name="Web App",
        page_node_id="7741:45837",
        page_name="Reach - auto content sharing",
        page_slug="reach-auto-content-sharing",
        figma_url="https://www.figma.com/design/hOV4QM?node-id=7741-45837",
        sections=sections or [],
        flows=flows or [],
        version="123456",
        last_modified="2026-03-31T11:01:12Z",
    )


def _make_entry(page_hash: str = "deadbeef12345678") -> PageEntry:
    return PageEntry(
        page_name="Reach - auto content sharing",
        page_slug="reach-auto-content-sharing",
        md_path="figma/hOV4QM/pages/reach-auto-content-sharing.md",
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T12:00:00Z",
    )


# --- render_page: frontmatter ---

def test_render_page_has_yaml_frontmatter():
    """INVARIANT: Rendered markdown starts with a valid YAML frontmatter block."""
    page = _make_page()
    md = render_page(page, _make_entry())
    assert md.startswith("---\n")
    assert "\n---\n" in md


def test_render_page_frontmatter_is_valid_figmapagefrontmatter():
    """INVARIANT: Frontmatter parses to a valid FigmaPageFrontmatter Pydantic model."""
    page = _make_page()
    md = render_page(page, _make_entry("deadbeef12345678"))
    fm = parse_frontmatter(md)
    assert fm is not None
    assert isinstance(fm, FigmaPageFrontmatter)


def test_render_page_frontmatter_carries_identity_fields():
    """INVARIANT: Frontmatter contains file_key, page_node_id, page_hash."""
    page = _make_page()
    md = render_page(page, _make_entry("deadbeef12345678"))
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.figmaclaw.file_key == "hOV4QM"
    assert fm.figmaclaw.page_node_id == "7741:45837"
    assert fm.figmaclaw.page_hash == "deadbeef12345678"


def test_render_page_frontmatter_carries_frame_descriptions():
    """INVARIANT: Frame descriptions appear in the frontmatter frames dict."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome screen", description="The onboarding welcome."),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = _make_page(sections=[section])
    md = render_page(page, _make_entry())
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.frames["welcome screen"] == "The onboarding welcome."


def test_render_page_frontmatter_omits_empty_descriptions():
    """INVARIANT: Frames with no description are not in frontmatter.frames."""
    frame = FigmaFrame(node_id="11:1", name="untitled frame", description="")
    section = FigmaSection(node_id="10:1", name="misc", frames=[frame])
    page = _make_page(sections=[section])
    md = render_page(page, _make_entry())
    fm = parse_frontmatter(md)
    assert fm is not None
    assert "untitled frame" not in fm.frames


# --- render_page: body ---

def test_render_page_has_h1_header():
    """INVARIANT: Body contains # {file_name} / {page_name}"""
    page = _make_page()
    md = render_page(page, _make_entry())
    assert "# Web App / Reach - auto content sharing" in md


def test_render_page_has_figma_url():
    """INVARIANT: Rendered markdown contains the Figma deep link."""
    page = _make_page()
    md = render_page(page, _make_entry())
    assert "https://www.figma.com/design/hOV4QM?node-id=7741-45837" in md


def test_render_page_section_heading_with_node_id():
    """INVARIANT: Each section appears as ## {name} (`{node_id}`)"""
    section = FigmaSection(node_id="10639:4378", name="schedule event", frames=[])
    page = _make_page(sections=[section])
    md = render_page(page, _make_entry())
    assert "## schedule event (`10639:4378`)" in md


def test_render_page_section_table_has_all_frames():
    """INVARIANT: Every frame appears in the section table with its node ID."""
    frames = [
        FigmaFrame(node_id="10635:89503", name="schedule / information box", description="Empty form."),
        FigmaFrame(node_id="10635:89347", name="schedule / socials enabled", description="Filled form."),
    ]
    section = FigmaSection(node_id="10639:4378", name="schedule event", frames=frames)
    page = _make_page(sections=[section])
    md = render_page(page, _make_entry())
    assert "`10635:89503`" in md
    assert "schedule / information box" in md
    assert "`10635:89347`" in md
    assert "schedule / socials enabled" in md


def test_render_page_uses_placeholder_in_table_when_no_description():
    """INVARIANT: Frames with empty description use a placeholder in the table row."""
    frame = FigmaFrame(node_id="11:1", name="untitled frame", description="")
    section = FigmaSection(node_id="10:1", name="misc", frames=[frame])
    page = _make_page(sections=[section])
    md = render_page(page, _make_entry())
    assert "(no description yet)" in md


def test_render_page_no_mermaid_when_no_flows():
    """INVARIANT: Mermaid block is absent when the page has no prototype flows."""
    page = _make_page(flows=[])
    md = render_page(page, _make_entry())
    assert "```mermaid" not in md


def test_render_page_has_mermaid_when_flows_present():
    """INVARIANT: Mermaid flowchart block present when flows exist."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome"),
        FigmaFrame(node_id="11:2", name="permissions"),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = _make_page(sections=[section], flows=[("11:1", "11:2")])
    md = render_page(page, _make_entry())
    assert "```mermaid" in md
    assert "flowchart" in md


def test_render_page_has_quick_reference_table():
    """INVARIANT: Quick Reference table present at end of document."""
    section = FigmaSection(
        node_id="10:1",
        name="auth",
        frames=[FigmaFrame(node_id="11:1", name="login", description="Login screen.")],
    )
    page = _make_page(sections=[section])
    md = render_page(page, _make_entry())
    assert "Quick Reference" in md


# --- figma_parse ---

def test_parse_frontmatter_from_rendered_output():
    """INVARIANT: parse_frontmatter recovers full FigmaPageFrontmatter from render_page output."""
    page = _make_page()
    entry = _make_entry("deadbeef12345678")
    md = render_page(page, entry)
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.figmaclaw.file_key == "hOV4QM"
    assert fm.figmaclaw.page_node_id == "7741:45837"
    assert fm.figmaclaw.page_hash == "deadbeef12345678"


def test_parse_page_metadata_from_rendered_output():
    """INVARIANT: parse_page_metadata recovers FigmaclawMeta written by render_page."""
    page = _make_page()
    entry = _make_entry("deadbeef12345678")
    md = render_page(page, entry)
    meta = parse_page_metadata(md)
    assert meta is not None
    assert meta.file_key == "hOV4QM"
    assert meta.page_node_id == "7741:45837"
    assert meta.page_hash == "deadbeef12345678"


def test_parse_page_metadata_returns_none_for_missing_frontmatter():
    """INVARIANT: parse_page_metadata returns None when no figmaclaw frontmatter found."""
    md = "# Just a plain markdown file\n\nNo metadata here."
    assert parse_page_metadata(md) is None


def test_parse_frame_descriptions_recovers_descriptions():
    """INVARIANT: parse_frame_descriptions recovers {frame_name: description} from rendered md."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome screen", description="The onboarding welcome."),
        FigmaFrame(node_id="11:2", name="permissions screen", description="Asks for camera access."),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = _make_page(sections=[section])
    md = render_page(page, _make_entry())
    descriptions = parse_frame_descriptions(md)
    assert descriptions["welcome screen"] == "The onboarding welcome."
    assert descriptions["permissions screen"] == "Asks for camera access."


def test_parse_frame_descriptions_empty_for_plain_file():
    """INVARIANT: parse_frame_descriptions returns empty dict for non-figmaclaw markdown."""
    descriptions = parse_frame_descriptions("# Random markdown\n\nNo tables here.")
    assert descriptions == {}
