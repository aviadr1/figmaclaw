"""Tests for figma_client.py.

INVARIANTS:
- FigmaClient always uses X-Figma-Token header (not Authorization: Bearer)
- Retries on 429 with Retry-After respect
- Retries on transient failures up to the configured attempt budget
- get_file_meta returns version, lastModified, and pages list
- get_page returns node tree for a single page
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from itertools import pairwise
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

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
                                {
                                    "id": "10635:89503",
                                    "name": "schedule / information box",
                                    "type": "FRAME",
                                    "children": [],
                                },
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
    """INVARIANT: get_file_meta returns a typed FileMetaResponse (figmaclaw#11)."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            return_value=httpx.Response(200, json=_meta_response())
        )
        async with FigmaClient(api_key="figd_test") as client:
            meta = await client.get_file_meta(FILE_KEY)

    assert meta.version == "123456"
    assert meta.lastModified == "2026-03-31T11:01:12Z"
    assert meta.name == "Web App"
    pages = meta.document.children
    assert len(pages) == 2
    assert pages[0].type == "CANVAS"


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
async def test_get_nodes_skips_null_entries():
    """INVARIANT: Figma may return null for missing node ids; callers get an empty map."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/nodes").mock(
            return_value=httpx.Response(200, json={"nodes": {PAGE_NODE_ID: None}})
        )
        async with FigmaClient(api_key="figd_test") as client:
            nodes = await client.get_nodes(FILE_KEY, [PAGE_NODE_ID], depth=100)

    assert nodes == {}


@pytest.mark.asyncio
async def test_get_nodes_can_request_full_geometry_subtree():
    """INVARIANT: audit fetchers can omit depth and request geometry=paths."""
    with respx.mock:
        route = respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/nodes").mock(
            return_value=httpx.Response(
                200,
                json={
                    "nodes": {
                        PAGE_NODE_ID: {
                            "document": {"id": PAGE_NODE_ID, "name": "Frame", "type": "FRAME"}
                        }
                    }
                },
            )
        )
        async with FigmaClient(api_key="figd_test") as client:
            nodes = await client.get_nodes(FILE_KEY, [PAGE_NODE_ID], depth=None, geometry="paths")

    assert nodes[PAGE_NODE_ID]["type"] == "FRAME"
    params = dict(route.calls[0].request.url.params)
    assert params["ids"] == PAGE_NODE_ID
    assert params["geometry"] == "paths"
    assert "depth" not in params


@pytest.mark.asyncio
async def test_get_nodes_response_preserves_component_metadata():
    """INVARIANT: fetch-nodes can enrich instances with publishable component keys."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/nodes").mock(
            return_value=httpx.Response(
                200,
                json={
                    "nodes": {
                        PAGE_NODE_ID: {
                            "document": {
                                "id": PAGE_NODE_ID,
                                "name": "Frame",
                                "type": "FRAME",
                                "children": [
                                    {
                                        "id": "1:2",
                                        "name": "Button",
                                        "type": "INSTANCE",
                                        "componentId": "99:1",
                                    }
                                ],
                            }
                        }
                    },
                    "components": {"99:1": {"key": "component-key"}},
                },
            )
        )
        async with FigmaClient(api_key="figd_test") as client:
            payload = await client.get_nodes_response(FILE_KEY, [PAGE_NODE_ID])

    assert payload["components"]["99:1"]["key"] == "component-key"


@pytest.mark.asyncio
async def test_get_nodes_response_returns_sanitized_nodes_envelope():
    """INVARIANT: the raw nodes envelope matches the payload validated by the client."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/nodes").mock(
            return_value=httpx.Response(
                200,
                json={
                    "nodes": {
                        PAGE_NODE_ID: {
                            "document": {"id": PAGE_NODE_ID, "name": "Frame", "type": "FRAME"}
                        },
                        "missing:node": None,
                    },
                    "components": {},
                },
            )
        )
        async with FigmaClient(api_key="figd_test") as client:
            payload = await client.get_nodes_response(FILE_KEY, [PAGE_NODE_ID, "missing:node"])

    assert payload["nodes"] == {
        PAGE_NODE_ID: {"document": {"id": PAGE_NODE_ID, "name": "Frame", "type": "FRAME"}}
    }


@pytest.mark.asyncio
async def test_retries_on_429():
    """INVARIANT: Client retries on 429 and eventually succeeds."""
    call_count = 0

    with respx.mock:

        def rate_limited_then_ok(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(
                    429, headers={"retry-after": "0"}, json={"err": "rate limited"}
                )
            return httpx.Response(200, json=_meta_response())

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            side_effect=rate_limited_then_ok
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with FigmaClient(api_key="figd_test") as client:
                meta = await client.get_file_meta(FILE_KEY)

    assert call_count == 3
    assert meta.version == "123456"


def test_default_rate_limit_matches_figma_tier_one_guidance():
    """INVARIANT: default client pacing is 15 RPM / 4s, not a burstier setting."""
    client = FigmaClient(api_key="figd_test")

    assert client._min_interval == 4.0


def test_client_retry_budget_is_configurable():
    """INVARIANT: live smoke can use a lower retry budget without changing production defaults."""
    default_client = FigmaClient(api_key="figd_test")
    smoke_client = FigmaClient(api_key="figd_test", timeout_s=30.0, max_attempts=3)

    assert default_client._timeout_s == 300.0
    assert default_client._max_attempts == 10
    assert smoke_client._timeout_s == 30.0
    assert smoke_client._max_attempts == 3


def test_client_rejects_empty_retry_budget():
    """INVARIANT: max_attempts=0 would make the request loop nonsensical."""
    with pytest.raises(ValueError, match="max_attempts"):
        FigmaClient(api_key="figd_test", max_attempts=0)


@pytest.mark.asyncio
async def test_concurrent_requests_are_paced_serially():
    """INVARIANT: concurrent callers share one pacing gate.

    Pull refreshes fetch page chunks concurrently. Without a lock around the
    pacer, every waiting task can wake at the same time and burst into Figma,
    which defeats the proactive rate limit and produces 429s.
    """
    request_times: list[float] = []

    with respx.mock:

        def record_request(request: httpx.Request) -> httpx.Response:
            request_times.append(time.monotonic())
            return httpx.Response(200, json=_meta_response())

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(side_effect=record_request)

        async with FigmaClient(api_key="figd_test", rate_limit_rpm=1200) as client:
            await asyncio.gather(
                client.get_file_meta(FILE_KEY),
                client.get_file_meta(FILE_KEY),
                client.get_file_meta(FILE_KEY),
            )

    assert len(request_times) == 3
    gaps = [b - a for a, b in pairwise(request_times)]
    assert min(gaps) >= 0.04


@pytest.mark.asyncio
async def test_retries_on_429_with_http_date_retry_after():
    """INVARIANT: Retry-After supports both seconds and HTTP-date forms."""
    call_count = 0
    retry_at = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)

    with respx.mock:

        def rate_limited_then_ok(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(429, headers={"retry-after": retry_at})
            return httpx.Response(200, json=_meta_response())

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            side_effect=rate_limited_then_ok
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            async with FigmaClient(api_key="figd_test") as client:
                meta = await client.get_file_meta(FILE_KEY)

    assert call_count == 2
    assert meta.version == "123456"
    assert sleep_mock.await_args_list
    assert max(call.args[0] for call in sleep_mock.await_args_list) >= 5


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

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            side_effect=server_error_then_ok
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with FigmaClient(api_key="figd_test") as client:
                meta = await client.get_file_meta(FILE_KEY)

    assert call_count == 2
    assert meta.version == "123456"


@pytest.mark.asyncio
async def test_5xx_retry_respects_configured_attempt_budget():
    """INVARIANT: CI smoke can fail fast instead of waiting for production retry depth."""
    call_count = 0

    with respx.mock:

        def server_error(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, json={"err": "server error"})

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(side_effect=server_error)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with FigmaClient(api_key="figd_test", max_attempts=3) as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await client.get_file_meta(FILE_KEY)

    assert call_count == 3


@pytest.mark.asyncio
async def test_429_does_not_sleep_after_final_attempt():
    """INVARIANT: exhausted retry budgets fail immediately on the final response."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(
            return_value=httpx.Response(429, headers={"retry-after": "60"}, json={})
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock:
            async with FigmaClient(api_key="figd_test", max_attempts=1) as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await client.get_file_meta(FILE_KEY)

    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_retries_on_connection_drop():
    """INVARIANT: Client retries on RemoteProtocolError (connection drops mid-transfer).

    Large file tree downloads sometimes fail with 'peer closed connection without
    sending complete message body'. The client should retry instead of failing.
    """
    call_count = 0

    with respx.mock:

        def conn_drop_then_ok(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body",
                )
            return httpx.Response(200, json=_meta_response())

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(side_effect=conn_drop_then_ok)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with FigmaClient(api_key="figd_test") as client:
                meta = await client.get_file_meta(FILE_KEY)

    assert call_count == 3
    assert meta.version == "123456"


@pytest.mark.asyncio
async def test_retries_on_read_timeout():
    """INVARIANT: Client retries on ReadTimeout (slow responses that time out)."""
    call_count = 0

    with respx.mock:

        def timeout_then_ok(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.ReadTimeout("read timeout")
            return httpx.Response(200, json=_meta_response())

        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}").mock(side_effect=timeout_then_ok)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            async with FigmaClient(api_key="figd_test") as client:
                meta = await client.get_file_meta(FILE_KEY)

    assert call_count == 2
    assert meta.version == "123456"


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
async def test_list_file_webhooks_returns_list():
    """INVARIANT: list_file_webhooks uses context=file query and returns list."""
    with respx.mock:
        route = respx.get("https://api.figma.com/v2/webhooks").mock(
            return_value=httpx.Response(200, json={"webhooks": [{"id": "wh1"}]})
        )
        async with FigmaClient(api_key="figd_test") as client:
            webhooks = await client.list_file_webhooks(file_key=FILE_KEY)

    assert isinstance(webhooks, list)
    assert webhooks[0]["id"] == "wh1"
    url = str(route.calls[0].request.url)
    assert "context=file" in url
    assert f"context_id={FILE_KEY}" in url


@pytest.mark.asyncio
async def test_create_file_webhook_posts_context_file_payload():
    """INVARIANT: create_file_webhook posts v2 payload with context=file + context_id."""
    with respx.mock:
        route = respx.post("https://api.figma.com/v2/webhooks").mock(
            return_value=httpx.Response(200, json={"id": "wh_file"})
        )
        async with FigmaClient(api_key="figd_test") as client:
            payload = await client.create_file_webhook(
                file_key=FILE_KEY,
                endpoint="https://example.com/hook",
                passcode="secret",
                description="desc",
            )

    assert payload["id"] == "wh_file"
    body = route.calls[0].request.content.decode()
    assert '"context":"file"' in body
    assert f'"context_id":"{FILE_KEY}"' in body


@pytest.mark.asyncio
async def test_create_webhook_posts_team_payload():
    """INVARIANT: create_webhook posts v2 payload with team_id field."""
    with respx.mock:
        route = respx.post("https://api.figma.com/v2/webhooks").mock(
            return_value=httpx.Response(200, json={"id": "wh_team"})
        )
        async with FigmaClient(api_key="figd_test") as client:
            payload = await client.create_webhook(
                team_id="team123",
                endpoint="https://example.com/hook",
                passcode="secret",
            )

    assert payload["id"] == "wh_team"
    body = route.calls[0].request.content.decode()
    assert '"team_id":"team123"' in body


@pytest.mark.asyncio
async def test_delete_webhook_calls_delete_endpoint():
    """INVARIANT: delete_webhook hits /v2/webhooks/{id}."""
    webhook_id = "wh_to_delete"
    with respx.mock:
        route = respx.delete(f"https://api.figma.com/v2/webhooks/{webhook_id}").mock(
            return_value=httpx.Response(200, json={})
        )
        async with FigmaClient(api_key="figd_test") as client:
            await client.delete_webhook(webhook_id)

    assert len(route.calls) == 1


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
    assert projects[0].name == "Web"
    assert projects[1].name == "Mobile"


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
    assert files[0].key == "abc123"
    assert files[0].name == "Web App"


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


@pytest.mark.asyncio
async def test_list_team_component_sets_paginates():
    """ERR-2: census can use one paginated team-library scan instead of per-file reads."""
    team_id = "team1"
    first_payload = {
        "meta": {
            "component_sets": [{"key": "cs1", "file_key": "file1", "name": "Button"}],
            "cursor": {"after": 30},
        }
    }
    second_payload = {
        "meta": {
            "component_sets": [{"key": "cs2", "file_key": "file2", "name": "Input"}],
            "cursor": {},
        }
    }
    with respx.mock:
        route = respx.get(f"https://api.figma.com/v1/teams/{team_id}/component_sets").mock(
            side_effect=[
                httpx.Response(200, json=first_payload),
                httpx.Response(200, json=second_payload),
            ]
        )
        async with FigmaClient(api_key="figd_test") as client:
            component_sets = await client.list_team_component_sets(team_id)

    assert [cs["key"] for cs in component_sets] == ["cs1", "cs2"]
    assert len(route.calls) == 2
    assert route.calls[0].request.url.params["page_size"] == "100"
    assert route.calls[1].request.url.params["after"] == "30"


# ---------------------------------------------------------------------------
# get_local_variables — canon §4 TC-1, §5 D14
# ---------------------------------------------------------------------------


def _local_variables_response() -> dict:
    """Synthetic /variables/local response with one collection, two modes,
    one COLOR variable with mode-specific values, and one FLOAT variable."""
    return {
        "status": 200,
        "error": False,
        "meta": {
            "variables": {
                "VariableID:abc/1:1": {
                    "id": "VariableID:abc/1:1",
                    "name": "fg/primary",
                    "key": "abc-fg-primary",
                    "variableCollectionId": "VariableCollectionId:abc/1:0",
                    "resolvedType": "COLOR",
                    "valuesByMode": {
                        "1:0": {"r": 0.06, "g": 0.15, "b": 0.22, "a": 1.0},
                        "1:1": {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0},
                    },
                    "remote": False,
                    "scopes": ["ALL_FILLS"],
                    "codeSyntax": {"WEB": "fg-primary"},
                },
                "VariableID:abc/2:1": {
                    "id": "VariableID:abc/2:1",
                    "name": "radius/md",
                    "key": "abc-radius-md",
                    "variableCollectionId": "VariableCollectionId:abc/1:0",
                    "resolvedType": "FLOAT",
                    "valuesByMode": {"1:0": 12.0, "1:1": 12.0},
                    "scopes": ["CORNER_RADIUS"],
                },
            },
            "variableCollections": {
                "VariableCollectionId:abc/1:0": {
                    "id": "VariableCollectionId:abc/1:0",
                    "name": "Primitives",
                    "key": "abc-primitives",
                    "modes": [
                        {"modeId": "1:0", "name": "Light"},
                        {"modeId": "1:1", "name": "Dark"},
                    ],
                    "defaultModeId": "1:0",
                    "variableIds": [
                        "VariableID:abc/1:1",
                        "VariableID:abc/2:1",
                    ],
                }
            },
        },
    }


@pytest.mark.asyncio
async def test_get_local_variables_returns_typed_response_on_success():
    """INVARIANT (TC-1): get_local_variables returns a typed LocalVariablesResponse
    with full identity per variable (name, collection, valuesByMode, scopes, codeSyntax)."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/variables/local").mock(
            return_value=httpx.Response(200, json=_local_variables_response())
        )
        async with FigmaClient(api_key="figd_test") as client:
            response = await client.get_local_variables(FILE_KEY)

    assert response is not None
    assert response.status == 200
    assert response.error is False
    assert len(response.meta.variables) == 2

    fg = response.meta.variables["VariableID:abc/1:1"]
    assert fg.name == "fg/primary"
    assert fg.resolvedType == "COLOR"
    assert set(fg.valuesByMode.keys()) == {"1:0", "1:1"}  # TC-4 mode-aware
    assert fg.scopes == ["ALL_FILLS"]
    assert fg.codeSyntax == {"WEB": "fg-primary"}

    coll = response.meta.variableCollections["VariableCollectionId:abc/1:0"]
    assert coll.name == "Primitives"
    assert coll.defaultModeId == "1:0"
    assert len(coll.modes) == 2
    assert coll.modes[0].name == "Light"


@pytest.mark.asyncio
async def test_get_local_variables_returns_none_on_403():
    """INVARIANT (D14): when Enterprise scope `file_variables:read` is not granted,
    Figma returns 403. The client maps this to None so callers can fall back to
    seeded:* catalog entries instead of crashing."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/variables/local").mock(
            return_value=httpx.Response(403, json={"status": 403, "error": True})
        )
        async with FigmaClient(api_key="figd_test") as client:
            response = await client.get_local_variables(FILE_KEY)

    assert response is None


@pytest.mark.asyncio
async def test_get_local_variables_with_reason_preserves_403_message():
    """INVARIANT ERR-1: callers can cache persistent REST scope failures."""
    message = (
        "Invalid scope(s): file_content:read, projects:read. "
        "This endpoint requires the file_variables:read scope"
    )
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/variables/local").mock(
            return_value=httpx.Response(
                403, json={"status": 403, "error": True, "message": message}
            )
        )
        async with FigmaClient(api_key="figd_test") as client:
            response, reason = await client.get_local_variables_with_reason(FILE_KEY)

    assert response is None
    assert reason == message


@pytest.mark.asyncio
async def test_get_local_variables_propagates_non_403_errors():
    """INVARIANT (LW-1): non-403 errors must NOT be silently swallowed (warn-and-drop).
    They propagate to the caller, which decides whether to retry or skip the file."""
    # Use 401 — never retried by the client, so the error surfaces directly.
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/variables/local").mock(
            return_value=httpx.Response(401, json={"status": 401, "error": True})
        )
        async with FigmaClient(api_key="figd_test") as client:  # noqa: SIM117
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await client.get_local_variables(FILE_KEY)

    assert exc_info.value.response.status_code == 401


@pytest.mark.asyncio
async def test_get_local_variables_handles_empty_meta():
    """INVARIANT: a file with no variables (empty meta) returns an empty,
    valid response — not None, not an error."""
    with respx.mock:
        respx.get(f"https://api.figma.com/v1/files/{FILE_KEY}/variables/local").mock(
            return_value=httpx.Response(200, json={"status": 200, "error": False, "meta": {}})
        )
        async with FigmaClient(api_key="figd_test") as client:
            response = await client.get_local_variables(FILE_KEY)

    assert response is not None
    assert response.meta.variables == {}
    assert response.meta.variableCollections == {}
