"""Smoke tests against the real Figma API.

Requires FIGMA_API_KEY env var. Run with:
    uv run pytest -m smoke

These tests are skipped by default in CI.
"""

from __future__ import annotations

import os

import pytest

from figmaclaw.figma_client import FigmaClient

# The Web App file used in linear-git
TEST_FILE_KEY = "hOV4QMBnDIG5s5OYkSrX9E"
# Reach - auto content sharing page
TEST_PAGE_NODE_ID = "7741:45837"


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

    assert meta["version"], "version must be non-empty"
    assert meta["lastModified"], "lastModified must be non-empty"
    pages = meta["document"]["children"]
    assert len(pages) > 0, "file must have at least one page"
    assert all(p["type"] == "CANVAS" for p in pages)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_page_returns_section_nodes(api_key: str) -> None:
    """Smoke: get_page returns CANVAS node with SECTION children."""
    async with FigmaClient(api_key=api_key) as client:
        page = await client.get_page(TEST_FILE_KEY, TEST_PAGE_NODE_ID)

    assert page["type"] == "CANVAS"
    assert page["name"] == "Reach - auto content sharing"
    children = page["children"]
    assert len(children) > 0
    section_types = {c["type"] for c in children}
    assert "SECTION" in section_types


PERSONAL_TEAM_ID = "1314645078360119627"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_list_webhooks_returns_list(api_key: str) -> None:
    """Smoke: list_webhooks returns a list (may be empty) for personal team."""
    async with FigmaClient(api_key=api_key) as client:
        webhooks = await client.list_webhooks(team_id=PERSONAL_TEAM_ID)

    assert isinstance(webhooks, list)
