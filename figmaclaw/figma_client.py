"""Async Figma REST API client.

Rate-limit pacing: Figma Tier 1 allows 15 req/min on Pro (Full seat).
The client enforces a minimum interval between requests (default 4s =
15 req/min) to avoid 429s proactively. If a 429 does occur, it respects
the Retry-After header. Set rate_limit_rpm=0 to disable pacing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import httpx


class FigmaClient:
    """Async client for the Figma REST API.

    Uses X-Figma-Token header (not Authorization: Bearer).
    Proactively paces requests to stay under Figma rate limits.
    Retries on 429 (rate limit) and 5xx errors with exponential backoff.
    """

    _base_url = "https://api.figma.com"

    def __init__(self, api_key: str, *, rate_limit_rpm: int = 14) -> None:
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._min_interval = 60.0 / rate_limit_rpm if rate_limit_rpm > 0 else 0.0
        self._last_request_time: float = 0.0

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"X-Figma-Token": self._api_key},
                timeout=120.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> FigmaClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _pace(self) -> None:
        """Sleep if needed to stay under the rate limit."""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET request with proactive pacing and retry on 429 / 5xx."""
        client = await self._ensure_client()
        url = f"{self._base_url}{path}"
        for attempt in range(10):
            await self._pace()
            response = await client.get(url, params=params)
            if response.status_code == 429:
                retry_after = int(response.headers.get("retry-after", "10"))
                await asyncio.sleep(max(retry_after, 5))
                continue
            if response.status_code >= 500 and attempt < 9:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        response.raise_for_status()
        return {}

    async def get_file_meta(self, file_key: str) -> dict[str, Any]:
        """GET /v1/files/{file_key}?depth=1 — cheap version + page list check."""
        return await self._get(f"/v1/files/{file_key}", params={"depth": "1"})

    async def get_page(self, file_key: str, page_node_id: str) -> dict[str, Any]:
        """GET /v1/files/{file_key}/nodes?ids={page_node_id} — single page tree.

        Returns the document node for the requested page (the CANVAS node),
        not the full wrapper response.
        """
        data = await self._get(
            f"/v1/files/{file_key}/nodes",
            params={"ids": page_node_id},
        )
        nodes: dict[str, Any] = data.get("nodes", {})
        entry = nodes.get(page_node_id, {})
        doc: dict[str, Any] = entry.get("document", {})
        return doc

    async def get_file_full(
        self, file_key: str, *, version: str | None = None,
    ) -> dict[str, Any]:
        """GET /v1/files/{file_key}[?version=...] — full file tree.

        If *version* is given, fetches the file state at that historical version.
        Use for initial tracking or version diffs — one call returns all pages
        and all frames, which is far cheaper than per-page fetches.
        """
        params = {"version": version} if version else None
        return await self._get(f"/v1/files/{file_key}", params=params)

    async def _get_url(self, url: str) -> dict[str, Any]:
        """GET an absolute Figma API URL (used for pagination)."""
        # Strip the base URL prefix if present
        if url.startswith(self._base_url):
            path_with_query = url[len(self._base_url):]
        else:
            path_with_query = url
        # httpx will handle the query string if embedded in the path
        client = await self._ensure_client()
        full_url = f"{self._base_url}{path_with_query}" if not url.startswith("http") else url
        for attempt in range(10):
            await self._pace()
            response = await client.get(full_url)
            if response.status_code == 429:
                retry_after = int(response.headers.get("retry-after", "10"))
                await asyncio.sleep(max(retry_after, 5))
                continue
            if response.status_code >= 500 and attempt < 9:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        response.raise_for_status()
        return {}

    async def get_versions(
        self,
        file_key: str,
        *,
        max_pages: int = 20,
        stop_when: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """GET /v1/files/{file_key}/versions — paginated version history.

        Returns all versions ordered newest-first. Follows pagination up to
        *max_pages* pages (default 20 pages × 50 per page = 1000 versions).

        If *stop_when* is provided, it should be a callable that takes a
        version dict and returns True when pagination should stop (e.g. when
        a version is older than a desired cutoff). The matching version and
        all newer ones are included in the result.
        """
        all_versions: list[dict[str, Any]] = []
        url: str | None = f"/v1/files/{file_key}/versions?page_size=50"
        for _ in range(max_pages):
            if not url:
                break
            data = await self._get_url(url)
            versions: list[dict[str, Any]] = data.get("versions", [])
            if not versions:
                break
            all_versions.extend(versions)
            # Check if we've found a version older than the cutoff
            if stop_when and any(stop_when(v) for v in versions):
                break
            pagination = data.get("pagination", {})
            next_page = pagination.get("next_page")
            url = next_page if next_page else None
        return all_versions

    async def get_page_at_version(
        self, file_key: str, page_node_id: str, version: str,
    ) -> dict[str, Any]:
        """GET /v1/files/{file_key}/nodes?ids={id}&version={v} — page tree at a version.

        Same as get_page() but for a historical version.
        """
        data = await self._get(
            f"/v1/files/{file_key}/nodes",
            params={"ids": page_node_id, "version": version},
        )
        nodes: dict[str, Any] = data.get("nodes", {})
        entry = nodes.get(page_node_id, {})
        doc: dict[str, Any] = entry.get("document", {})
        return doc

    async def list_team_projects(self, team_id: str) -> list[dict[str, Any]]:
        """GET /v1/teams/{team_id}/projects — list projects for a team."""
        data = await self._get(f"/v1/teams/{team_id}/projects")
        result: list[dict[str, Any]] = data.get("projects", [])
        return result

    async def list_project_files(self, project_id: str) -> list[dict[str, Any]]:
        """GET /v1/projects/{project_id}/files — list files in a project."""
        data = await self._get(f"/v1/projects/{project_id}/files")
        result: list[dict[str, Any]] = data.get("files", [])
        return result

    async def get_image_urls(
        self,
        file_key: str,
        node_ids: list[str],
        *,
        scale: float = 0.5,
        format: str = "png",
    ) -> dict[str, str | None]:
        """GET /v1/images/{file_key}?ids=... — batch image export URLs.

        Returns {node_id: url_or_none}. The URL is a temporary S3 link — download promptly.
        Figma sometimes returns IDs with "-" instead of ":" — normalised back to ":" on return.
        """
        data = await self._get(
            f"/v1/images/{file_key}",
            params={"ids": ",".join(node_ids), "scale": str(scale), "format": format},
        )
        raw: dict[str, str | None] = data.get("images", {})
        return {k.replace("-", ":"): v for k, v in raw.items()}

    async def download_url(self, url: str) -> bytes:
        """Download an arbitrary URL (e.g. Figma S3 image export). Not a Figma API endpoint."""
        client = await self._ensure_client()
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        content: bytes = response.content
        return content

    async def list_webhooks(self, team_id: str) -> list[dict[str, Any]]:
        """GET /v2/teams/{team_id}/webhooks — list webhooks for a team."""
        data = await self._get(f"/v2/teams/{team_id}/webhooks")
        result: list[dict[str, Any]] = data.get("webhooks", [])
        return result

    async def create_webhook(
        self,
        team_id: str,
        endpoint: str,
        passcode: str,
        event_type: str = "FILE_UPDATE",
    ) -> dict[str, Any]:
        """POST /v2/webhooks — register a webhook for a team."""
        client = await self._ensure_client()
        response = await client.post(
            f"{self._base_url}/v2/webhooks",
            json={
                "event_type": event_type,
                "team_id": team_id,
                "endpoint": endpoint,
                "passcode": passcode,
            },
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def delete_webhook(self, webhook_id: str) -> None:
        """DELETE /v2/webhooks/{webhook_id} — remove a webhook."""
        client = await self._ensure_client()
        response = await client.delete(f"{self._base_url}/v2/webhooks/{webhook_id}")
        response.raise_for_status()
