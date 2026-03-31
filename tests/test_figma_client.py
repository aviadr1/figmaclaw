"""Tests for figma_client.py.

INVARIANTS:
- FigmaClient always uses X-Figma-Token header (not Authorization: Bearer)
- Retries on 429 with Retry-After respect
- Retries on 5xx up to 5 attempts
- get_file_meta returns version, lastModified, and pages list
- get_page returns node tree for a single page
"""

from __future__ import annotations

import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from figmaclaw.figma_client import FigmaClient


FILE_KEY = "testFileKey123"
PAGE_NODE_ID = "7741:45837"


def _meta_response() -> dict:
    return {
        "name": "Web App",
        "version": "123456",
        "lastModified": "2026-03-31T11:01:12Z",
        "document": {
            "id": "0:0",
            "name": "Document",
            "type": "DOCUMENT",
            "children": [
                {"id": "0:1", "name": "Page 1", "type": "CANVAS"},
                {"id": "0:2", "name": "Page 2", "type": "CANVAS"},
            ],
        },
    }


def _page_response() -> dict:
    return {
        "nodes": {
            PAGE_NODE_ID: {
                "document": {
                    "id": PAGE_NODE_ID,
                    "name": "Reach - auto content sharing",
                    "type": "CANVAS",
                    "children": [
                        {
                            "id": "10639:4378",
                            "name": "schedule event",
                            "type": "SECTION",
                            "children": [
                                {"id": "10635:89503", "name": "schedule / information box", "type": "FRAME", "children": []},
                            ],
                        }
                    ],
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_get_file_meta_uses_x_figma_token_header():
    """INVARIANT: FigmaClient must use X-Figma-Token, not Authorization: Bearer."""
    with respx.mock:
        route = respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            return_value=httpx.Response(200, json=_meta_response())
        )
        async with FigmaClient(api_key="figd_test") as client:
            await client.get_file_meta(FILE_KEY)

        request = route.calls[0].request
        assert "X-Figma-Token" in request.headers
        assert request.headers["X-Figma-Token"] == "figd_test"
        assert "authorization" not in request.headers


@pytest.mark.asyncio
async def test_get_file_meta_returns_version_and_pages():
    """INVARIANT: get_file_meta returns dict with version, lastModified, document.children."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            return_value=httpx.Response(200, json=_meta_response())
        )
        async with FigmaClient(api_key="figd_test") as client:
            meta = await client.get_file_meta(FILE_KEY)

    assert meta["version"] == "123456"
    assert meta["lastModified"] == "2026-03-31T11:01:12Z"
    assert meta["name"] == "Web App"
    pages = meta["document"]["children"]
    assert len(pages) == 2
    assert pages[0]["type"] == "CANVAS"


@pytest.mark.asyncio
async def test_get_file_meta_passes_depth_1():
    """INVARIANT: get_file_meta uses depth=1 for a cheap call (no deep node tree)."""
    with respx.mock:
        route = respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            return_value=httpx.Response(200, json=_meta_response())
        )
        async with FigmaClient(api_key="figd_test") as client:
            await client.get_file_meta(FILE_KEY)

    assert "depth=1" in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_get_page_returns_node_tree():
    """INVARIANT: get_page returns the full node tree for a single page."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/nodes").mock(
            return_value=httpx.Response(200, json=_page_response())
        )
        async with FigmaClient(api_key="figd_test") as client:
            node = await client.get_page(FILE_KEY, PAGE_NODE_ID)

    assert node["id"] == PAGE_NODE_ID
    assert node["type"] == "CANVAS"
    assert len(node["children"]) == 1


@pytest.mark.asyncio
async def test_get_page_passes_node_id_as_ids_param():
    """INVARIANT: get_page must request the specific page node, not the full file."""
    with respx.mock:
        route = respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/nodes").mock(
            return_value=httpx.Response(200, json=_page_response())
        )
        async with FigmaClient(api_key="figd_test") as client:
            await client.get_page(FILE_KEY, PAGE_NODE_ID)

    url_str = str(route.calls[0].request.url)
    assert "ids=" in url_str
    assert PAGE_NODE_ID.replace(":", "%3A") in url_str or PAGE_NODE_ID in url_str


@pytest.mark.asyncio
async def test_retries_on_429():
    """INVARIANT: Client retries on 429 and eventually succeeds."""
    call_count = 0

    with respx.mock:
        def rate_limited_then_ok(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(429, headers={"retry-after": "0"}, json={"err": "rate limited"})
            return httpx.Response(200, json=_meta_response())

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(side_effect=rate_limited_then_ok)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with FigmaClient(api_key="figd_test") as client:
                meta = await client.get_file_meta(FILE_KEY)

    assert call_count == 3
    assert meta["version"] == "123456"


@pytest.mark.asyncio
async def test_retries_on_5xx():
    """INVARIANT: Client retries on 5xx errors and eventually succeeds."""
    call_count = 0

    with respx.mock:
        def server_error_then_ok(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return httpx.Response(500, json={"err": "server error"})
            return httpx.Response(200, json=_meta_response())

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(side_effect=server_error_then_ok)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with FigmaClient(api_key="figd_test") as client:
                meta = await client.get_file_meta(FILE_KEY)

    assert call_count == 2
    assert meta["version"] == "123456"


@pytest.mark.asyncio
async def test_list_webhooks_returns_list():
    """INVARIANT: list_webhooks returns a list (possibly empty) via team endpoint."""
    team_id = "team123"
    with respx.mock:
        respx.get(f"https://api.figma.com/v2/teams/{team_id}/webhooks").mock(
            return_value=httpx.Response(200, json={"webhooks": []})
        )
        async with FigmaClient(api_key="figd_test") as client:
            webhooks = await client.list_webhooks(team_id=team_id)

    assert isinstance(webhooks, list)


@pytest.mark.asyncio
async def test_list_team_projects_returns_projects():
    """INVARIANT: list_team_projects returns the projects list from the API."""
    team_id = "1314617533998771588"
    projects_payload = {
        "projects": [
            {"id": "proj1", "name": "Web"},
            {"id": "proj2", "name": "Mobile"},
        ]
    }
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/teams/{team_id}/projects").mock(
            return_value=httpx.Response(200, json=projects_payload)
        )
        async with FigmaClient(api_key="figd_test") as client:
            projects = await client.list_team_projects(team_id)

    assert len(projects) == 2
    assert projects[0]["name"] == "Web"
    assert projects[1]["name"] == "Mobile"


@pytest.mark.asyncio
async def test_list_project_files_returns_files():
    """INVARIANT: list_project_files returns the files list for a given project."""
    project_id = "proj1"
    files_payload = {
        "files": [
            {"key": "abc123", "name": "Web App", "last_modified": "2026-03-01T00:00:00Z"},
        ]
    }
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/projects/{project_id}/files").mock(
            return_value=httpx.Response(200, json=files_payload)
        )
        async with FigmaClient(api_key="figd_test") as client:
            files = await client.list_project_files(project_id)

    assert len(files) == 1
    assert files[0]["key"] == "abc123"
    assert files[0]["name"] == "Web App"


@pytest.mark.asyncio
async def test_list_project_files_returns_empty_list_when_none():
    """INVARIANT: list_project_files returns [] when the API response has no files key."""
    project_id = "empty_proj"
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/projects/{project_id}/files").mock(
            return_value=httpx.Response(200, json={})
        )
        async with FigmaClient(api_key="figd_test") as client:
            files = await client.list_project_files(project_id)

    assert files == []
