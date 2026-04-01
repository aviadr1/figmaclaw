"""Tests for figma_render.py and figma_parse.py.

INVARIANTS:
- scaffold_page produces YAML frontmatter with FigmaPageFrontmatter schema
- Frontmatter carries file_key, page_node_id, and frame descriptions (flat schema)
- page_hash is NOT in frontmatter (manifest only)
- Body has H1 header, Figma URL, section tables, optional Mermaid
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
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_parse import parse_frame_descriptions, parse_frontmatter
from figmaclaw.figma_sync_state import PageEntry
import yaml


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


# --- scaffold_page: frontmatter ---

def test_scaffold_page_has_yaml_frontmatter():
    """INVARIANT: Rendered markdown starts with a valid YAML frontmatter block."""
    page = _make_page()
    md = scaffold_page(page, _make_entry())
    assert md.startswith("---\n")
    assert "\n---\n" in md


def test_scaffold_page_frontmatter_is_valid_figmapagefrontmatter():
    """INVARIANT: Frontmatter parses to a valid FigmaPageFrontmatter Pydantic model."""
    page = _make_page()
    md = scaffold_page(page, _make_entry("deadbeef12345678"))
    fm = parse_frontmatter(md)
    assert fm is not None
    assert isinstance(fm, FigmaPageFrontmatter)


def test_scaffold_page_frontmatter_carries_identity_fields():
    """INVARIANT: Frontmatter contains file_key and page_node_id (flat schema, no page_hash)."""
    page = _make_page()
    md = scaffold_page(page, _make_entry("deadbeef12345678"))
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.file_key == "hOV4QM"
    assert fm.page_node_id == "7741:45837"


def test_scaffold_page_frontmatter_carries_frame_descriptions():
    """INVARIANT: Frame descriptions appear in frontmatter keyed by node_id."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome screen", description="The onboarding welcome."),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = _make_page(sections=[section])
    md = scaffold_page(page, _make_entry())
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.frames["11:1"] == "The onboarding welcome."


def test_scaffold_page_frontmatter_omits_empty_descriptions():
    """INVARIANT: Frames with no description are not in frontmatter.frames."""
    frame = FigmaFrame(node_id="11:1", name="untitled frame", description="")
    section = FigmaSection(node_id="10:1", name="misc", frames=[frame])
    page = _make_page(sections=[section])
    md = scaffold_page(page, _make_entry())
    fm = parse_frontmatter(md)
    assert fm is not None
    assert "untitled frame" not in fm.frames


# --- scaffold_page: body ---

def test_scaffold_page_has_h1_header():
    """INVARIANT: Body contains # {file_name} / {page_name}"""
    page = _make_page()
    md = scaffold_page(page, _make_entry())
    assert "# Web App / Reach - auto content sharing" in md


def test_scaffold_page_has_figma_url():
    """INVARIANT: Rendered markdown contains the Figma deep link."""
    page = _make_page()
    md = scaffold_page(page, _make_entry())
    assert "https://www.figma.com/design/hOV4QM?node-id=7741-45837" in md


def test_scaffold_page_section_heading_with_node_id():
    """INVARIANT: Each section appears as ## {name} (`{node_id}`)"""
    section = FigmaSection(node_id="10639:4378", name="schedule event", frames=[])
    page = _make_page(sections=[section])
    md = scaffold_page(page, _make_entry())
    assert "## schedule event (`10639:4378`)" in md


def test_scaffold_page_section_table_has_all_frames():
    """INVARIANT: Every frame appears in the section table with its node ID."""
    frames = [
        FigmaFrame(node_id="10635:89503", name="schedule / information box", description="Empty form."),
        FigmaFrame(node_id="10635:89347", name="schedule / socials enabled", description="Filled form."),
    ]
    section = FigmaSection(node_id="10639:4378", name="schedule event", frames=frames)
    page = _make_page(sections=[section])
    md = scaffold_page(page, _make_entry())
    assert "`10635:89503`" in md
    assert "schedule / information box" in md
    assert "`10635:89347`" in md
    assert "schedule / socials enabled" in md


def test_scaffold_page_uses_placeholder_in_table_when_no_description():
    """INVARIANT: Frames with empty description use a placeholder in the table row."""
    frame = FigmaFrame(node_id="11:1", name="untitled frame", description="")
    section = FigmaSection(node_id="10:1", name="misc", frames=[frame])
    page = _make_page(sections=[section])
    md = scaffold_page(page, _make_entry())
    assert "(no description yet)" in md


def test_scaffold_page_no_mermaid_when_no_flows():
    """INVARIANT: Mermaid block is absent when the page has no prototype flows."""
    page = _make_page(flows=[])
    md = scaffold_page(page, _make_entry())
    assert "```mermaid" not in md


def test_scaffold_page_has_mermaid_when_flows_present():
    """INVARIANT: Mermaid flowchart block present when flows exist."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome"),
        FigmaFrame(node_id="11:2", name="permissions"),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = _make_page(sections=[section], flows=[("11:1", "11:2")])
    md = scaffold_page(page, _make_entry())
    assert "```mermaid" in md
    assert "flowchart" in md


def test_scaffold_page_has_no_quick_reference_table():
    """INVARIANT: Quick Reference table is not rendered — data is in frontmatter frames dict."""
    section = FigmaSection(
        node_id="10:1",
        name="auth",
        frames=[FigmaFrame(node_id="11:1", name="login", description="Login screen.")],
    )
    page = _make_page(sections=[section])
    md = scaffold_page(page, _make_entry())
    assert "Quick Reference" not in md


# --- figma_parse ---

def test_parse_frontmatter_from_rendered_output():
    """INVARIANT: parse_frontmatter recovers FigmaPageFrontmatter (flat schema) from scaffold_page output."""
    page = _make_page()
    entry = _make_entry("deadbeef12345678")
    md = scaffold_page(page, entry)
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.file_key == "hOV4QM"
    assert fm.page_node_id == "7741:45837"


def test_parse_frontmatter_from_rendered_output():
    """INVARIANT: parse_frontmatter returns FigmaPageFrontmatter with file_key and page_node_id."""
    page = _make_page()
    entry = _make_entry("deadbeef12345678")
    md = scaffold_page(page, entry)
    meta = parse_frontmatter(md)
    assert meta is not None
    assert meta.file_key == "hOV4QM"
    assert meta.page_node_id == "7741:45837"


def test_parse_frontmatter_returns_none_for_missing_frontmatter():
    """INVARIANT: parse_frontmatter returns None when no figmaclaw frontmatter found."""
    md = "# Just a plain markdown file\n\nNo metadata here."
    assert parse_frontmatter(md) is None


def test_scaffold_page_frontmatter_is_compact_flow_style():
    """INVARIANT: frames and flows are single-line YAML flow style in frontmatter.

    Both must appear on exactly one line each, using inline { } / [ ] notation.
    This keeps the frontmatter compact and clearly machine-readable while the
    body prose remains human-readable. PyYAML must not wrap long values.
    """
    frames = [FigmaFrame(node_id="11:1", name="welcome", description="Welcome screen.")]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = _make_page(sections=[section], flows=[("11:1", "11:2")])
    md = scaffold_page(page, _make_entry())
    fm_block = md.split("---\n")[1]  # content between first two ---
    lines = fm_block.strip().splitlines()
    frames_lines = [l for l in lines if l.startswith("frames:")]
    flows_lines = [l for l in lines if l.startswith("flows:")]
    assert len(frames_lines) == 1, "frames must be on a single line (no multi-line block style)"
    assert len(flows_lines) == 1, "flows must be on a single line (no multi-line block style)"
    # Both must use inline flow-style notation (curly/square brackets)
    assert "{" in frames_lines[0], "frames must use inline flow-style {}: not block indented YAML"
    assert "[" in flows_lines[0], "flows must use inline flow-style []: not block indented YAML"
    # Verify the frontmatter round-trips correctly via yaml.safe_load
    data = yaml.safe_load(fm_block)
    assert data["frames"]["11:1"] == "Welcome screen."
    assert data["flows"] == [["11:1", "11:2"]]


def test_scaffold_page_frontmatter_flow_style_no_wrapping():
    """INVARIANT: frames stays on one line even with long descriptions containing apostrophes/colons.

    PyYAML's default width=80 would wrap long flow-style values across multiple lines,
    producing ugly and hard-to-diff frontmatter. width=2**20 prevents this.
    """
    long_desc = (
        "Live stream prepare screen with the camera showing a man's face and the "
        "topic field filled in as 'Design for AI': Shows recording-on and public-visibility "
        "settings with a pink 'Go Live Now' button at the bottom."
    )
    frame = FigmaFrame(node_id="11:1", name="prepare", description=long_desc)
    section = FigmaSection(node_id="10:1", name="Going live", frames=[frame])
    md = scaffold_page(_make_page(sections=[section]), _make_entry())
    fm_block = md.split("---\n")[1]
    frames_lines = [l for l in fm_block.strip().splitlines() if l.startswith("frames:")]
    assert len(frames_lines) == 1, "frames must stay on one line regardless of description length"
    # Round-trip must recover the exact description including apostrophes and colons
    data = yaml.safe_load(fm_block)
    assert data["frames"]["11:1"] == long_desc


def test_scaffold_page_frontmatter_no_page_hash():
    """INVARIANT: page_hash is NOT stored in the .md frontmatter (manifest only)."""
    page = _make_page()
    md = scaffold_page(page, _make_entry("deadbeef12345678"))
    fm_block = md.split("---\n")[1]
    assert "page_hash" not in fm_block
    assert "deadbeef" not in fm_block


def test_parse_frame_descriptions_recovers_descriptions():
    """INVARIANT: parse_frame_descriptions recovers {node_id: description} from rendered md."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome screen", description="The onboarding welcome."),
        FigmaFrame(node_id="11:2", name="permissions screen", description="Asks for camera access."),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = _make_page(sections=[section])
    md = scaffold_page(page, _make_entry())
    descriptions = parse_frame_descriptions(md)
    assert descriptions["11:1"] == "The onboarding welcome."
    assert descriptions["11:2"] == "Asks for camera access."


def test_parse_frame_descriptions_empty_for_plain_file():
    """INVARIANT: parse_frame_descriptions returns empty dict for non-figmaclaw markdown."""
    descriptions = parse_frame_descriptions("# Random markdown\n\nNo tables here.")
    assert descriptions == {}


# --- scaffold_page: component library sections skipped ---

def test_scaffold_page_skips_component_library_sections():
    """INVARIANT: scaffold_page omits component library sections — they get their own files."""
    from figmaclaw.figma_models import FigmaSection, FigmaFrame
    comp_section = FigmaSection(
        node_id="20:1",
        name="Buttons",
        frames=[FigmaFrame(node_id="30:1", name="Button / Primary", description="Primary CTA.")],
        is_component_library=True,
    )
    screen_section = FigmaSection(
        node_id="10:1",
        name="Onboarding",
        frames=[FigmaFrame(node_id="11:1", name="welcome", description="Welcome screen.")],
    )
    page = _make_page(sections=[screen_section, comp_section])
    md = scaffold_page(page, _make_entry())

    assert "Onboarding" in md          # screen section present
    assert "welcome" in md
    assert "Buttons" not in md         # component section absent
    assert "Button / Primary" not in md


def test_scaffold_page_omits_component_frame_descriptions_from_frontmatter():
    """INVARIANT: Component frame descriptions are not in the page frontmatter.frames."""
    from figmaclaw.figma_models import FigmaSection, FigmaFrame
    comp_section = FigmaSection(
        node_id="20:1",
        name="Buttons",
        frames=[FigmaFrame(node_id="30:1", name="Button / Primary", description="Primary CTA.")],
        is_component_library=True,
    )
    page = _make_page(sections=[comp_section])
    md = scaffold_page(page, _make_entry())
    fm = parse_frontmatter(md)
    assert fm is not None
    # Component descriptions must not leak into the page frontmatter
    assert "30:1" not in fm.frames


# --- render_component_section ---

def _make_component_section() -> tuple["FigmaSection", FigmaPage]:
    from figmaclaw.figma_models import FigmaSection, FigmaFrame
    section = FigmaSection(
        node_id="20:1",
        name="Buttons",
        frames=[
            FigmaFrame(node_id="30:1", name="Button / Primary", description="Primary CTA button."),
            FigmaFrame(node_id="30:2", name="Button / Secondary", description=""),
        ],
        is_component_library=True,
    )
    page = FigmaPage(
        file_key="AZswXf",
        file_name="Design System",
        page_node_id="5678:1234",
        page_name="Core Components",
        page_slug="core-components-5678-1234",
        figma_url="https://www.figma.com/design/AZswXf?node-id=5678-1234",
        sections=[section],
        flows=[],
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
    )
    return section, page


def test_render_component_section_has_yaml_frontmatter():
    """INVARIANT: Component .md starts with valid YAML frontmatter."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    assert md.startswith("---\n")
    assert "\n---\n" in md


def test_render_component_section_frontmatter_has_section_node_id():
    """INVARIANT: Component frontmatter carries section_node_id for direct Figma navigation."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.section_node_id == "20:1"


def test_render_component_section_frontmatter_carries_identity_fields():
    """INVARIANT: Component frontmatter carries file_key and page_node_id (flat schema, no page_hash)."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.file_key == "AZswXf"
    assert fm.page_node_id == "5678:1234"


def test_render_component_section_title_includes_page_and_section():
    """INVARIANT: Component .md title is '{file} / {page} / {section}' for unambiguous lookup."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    assert "# Design System / Core Components / Buttons" in md


def test_render_component_section_has_variants_table():
    """INVARIANT: Component .md has a Variants table listing all component nodes."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    assert "## Variants" in md
    assert "| Variant | Node ID | Description |" in md
    assert "Button / Primary" in md
    assert "`30:1`" in md


def test_render_component_section_uses_placeholder_for_empty_description():
    """INVARIANT: Frames with no description show placeholder in the Variants table."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    assert "(no description yet)" in md


def test_render_component_section_stores_descriptions_in_frontmatter():
    """INVARIANT: Component descriptions appear in frontmatter.frames keyed by node_id."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    fm = parse_frontmatter(md)
    assert fm is not None
    assert fm.frames["30:1"] == "Primary CTA button."
    assert fm.frames["30:2"] == ""  # all frames tracked in frontmatter; empty string until described


def test_render_component_section_has_no_mermaid():
    """INVARIANT: Component .md never contains a Mermaid flowchart (components don't have flows)."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    assert "```mermaid" not in md


def test_render_component_section_figma_url_points_to_section():
    """INVARIANT: Component .md Figma link targets the section node, not the page."""
    from figmaclaw.figma_render import render_component_section
    section, page = _make_component_section()
    md = render_component_section(section, page)
    # Section node ID "20:1" → "20-1" in URL
    assert "node-id=20-1" in md
