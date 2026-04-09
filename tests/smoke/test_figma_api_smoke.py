"""Smoke tests against the real Figma API.

Requires FIGMA_API_KEY env var. Run with:
    uv run pytest -m smoke

These tests are skipped by default in CI.
"""

from __future__ import annotations

import os

import pytest

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_frontmatter import FigmaPageFrontmatter
from figmaclaw.figma_models import FigmaPage, FigmaSection, from_page_node
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import PageEntry

# The Web App file used in linear-git
TEST_FILE_KEY = "hOV4QMBnDIG5s5OYkSrX9E"
# Reach - auto content sharing page
TEST_PAGE_NODE_ID = "7741:45837"
# Confirmed from live API: 8 SECTION children on this page
EXPECTED_SECTION_COUNT = 8

PERSONAL_TEAM_ID = "1314645078360119627"


@pytest.fixture
def api_key() -> str:
    key = os.environ.get("FIGMA_API_KEY", "")
    if not key:
        pytest.skip("FIGMA_API_KEY not set")
    return key


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_file_meta_returns_version(api_key: str) -> None:
    """Smoke: get_file_meta hits real API and returns version + pages."""
    async with FigmaClient(api_key=api_key) as client:
        meta = await client.get_file_meta(TEST_FILE_KEY)

    assert meta.version, "version must be non-empty"
    assert meta.lastModified, "lastModified must be non-empty"
    pages = meta.document.children
    assert len(pages) > 0, "file must have at least one page"
    assert all(p.type == "CANVAS" for p in pages)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_page_returns_canvas_with_sections(api_key: str) -> None:
    """Smoke: get_page returns the CANVAS document node directly with SECTION children."""
    async with FigmaClient(api_key=api_key) as client:
        page_node = await client.get_page(TEST_FILE_KEY, TEST_PAGE_NODE_ID)

    assert page_node["type"] == "CANVAS"
    assert page_node["name"] == "Reach - auto content sharing"
    children = page_node["children"]
    assert len(children) > 0
    section_types = {c["type"] for c in children}
    assert "SECTION" in section_types


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_from_page_node_matches_real_api_structure(api_key: str) -> None:
    """Smoke: from_page_node builds a FigmaPage with the correct number of sections.

    INVARIANT: The model structure must match what the real Figma API returns.
    Confirmed from live API: this page has exactly 8 SECTION nodes.
    """
    async with FigmaClient(api_key=api_key) as client:
        meta = await client.get_file_meta(TEST_FILE_KEY)
        file_name = meta.name
        page_node = await client.get_page(TEST_FILE_KEY, TEST_PAGE_NODE_ID)

    page = from_page_node(page_node, file_key=TEST_FILE_KEY, file_name=file_name)

    assert isinstance(page, FigmaPage)
    assert page.file_key == TEST_FILE_KEY
    assert page.file_name == file_name
    assert page.page_node_id == TEST_PAGE_NODE_ID
    assert page.page_name == "Reach - auto content sharing"
    assert len(page.sections) == EXPECTED_SECTION_COUNT, (
        f"Expected {EXPECTED_SECTION_COUNT} sections, got {len(page.sections)}. "
        f"Section names: {[s.name for s in page.sections]}"
    )
    # Every section must have at least one frame
    for section in page.sections:
        assert isinstance(section, FigmaSection)
        assert len(section.frames) > 0, f"Section {section.name!r} has no frames"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_render_and_parse_round_trip_against_real_page(api_key: str) -> None:
    """Smoke: scaffold_page + parse_frontmatter round-trips correctly for a real Figma page.

    INVARIANT: The YAML frontmatter written by scaffold_page must be parseable
    by parse_frontmatter into a valid FigmaPageFrontmatter with correct identity fields.
    """
    async with FigmaClient(api_key=api_key) as client:
        meta = await client.get_file_meta(TEST_FILE_KEY)
        file_name = meta.name
        page_node = await client.get_page(TEST_FILE_KEY, TEST_PAGE_NODE_ID)

    page = from_page_node(page_node, file_key=TEST_FILE_KEY, file_name=file_name)
    entry = PageEntry(
        page_name=page.page_name,
        page_slug="reach-auto-content-sharing",
        md_path="figma/web-app/pages/reach-auto-content-sharing.md",
        page_hash="smoketest00000000",
        last_refreshed_at="2026-03-31T00:00:00Z",
    )

    md = scaffold_page(page, entry)

    # Must start with frontmatter
    assert md.startswith("---\n"), "Rendered markdown must start with YAML frontmatter"

    # Frontmatter must parse to a valid FigmaPageFrontmatter
    fm = parse_frontmatter(md)
    assert fm is not None, "parse_frontmatter returned None — frontmatter is invalid"
    assert isinstance(fm, FigmaPageFrontmatter)
    assert fm.file_key == TEST_FILE_KEY
    assert fm.page_node_id == TEST_PAGE_NODE_ID

    # Body must have the H1 header and section tables
    assert "# " in md
    assert "| Screen |" in md


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_list_webhooks_returns_list(api_key: str) -> None:
    """Smoke: list_webhooks returns a list (may be empty) for personal team."""
    async with FigmaClient(api_key=api_key) as client:
        webhooks = await client.list_webhooks(team_id=PERSONAL_TEAM_ID)

    assert isinstance(webhooks, list)
