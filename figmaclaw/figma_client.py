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

from figmaclaw.figma_api_models import (
    FileMetaResponse,
    FileSummary,
    NodesResponse,
    ProjectFilesResponse,
    ProjectSummary,
    TeamProjectsResponse,
    VersionsPage,
    VersionSummary,
    _validate,
)


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
        """GET request with proactive pacing and retry on 429 / 5xx / connection errors."""
        client = await self._ensure_client()
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(10):
            await self._pace()
            try:
                response = await client.get(url, params=params)
            except (httpx.RemoteProtocolError, httpx.ReadError,
                    httpx.ReadTimeout, httpx.ConnectError) as e:
                # Connection dropped mid-transfer — retry with backoff.
                # Large file trees (e.g. 50+ pages) sometimes get truncated.
                last_exc = e
                if attempt < 9:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise
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
        if last_exc:
            raise last_exc
        response.raise_for_status()
        return {}

    async def get_file_meta(self, file_key: str) -> FileMetaResponse:
        """GET /v1/files/{file_key}?depth=1 — cheap version + page list check.

        Returns a validated :class:`FileMetaResponse`. If Figma's response
        is missing a required field (``name``, ``version``, ``lastModified``)
        the call raises :class:`figma_api_models.FigmaAPIValidationError`
        with the file_key in the message — figmaclaw#11.
        """
        data = await self._get(f"/v1/files/{file_key}", params={"depth": "1"})
        return _validate(
            FileMetaResponse, data,
            endpoint="GET /v1/files/{key}?depth=1",
            context=f"file_key={file_key}",
        )

    async def get_page(self, file_key: str, page_node_id: str) -> dict[str, Any]:
        """GET /v1/files/{file_key}/nodes?ids={page_node_id} — single page tree.

        Returns the document node for the requested page (the CANVAS node),
        not the full wrapper response. The recursive document tree stays as
        :class:`dict` because :func:`figmaclaw.figma_models.from_page_node`
        walks it with its own conventions — see the design note in
        :mod:`figmaclaw.figma_api_models`.

        The wrapper envelope *is* validated so a Figma schema drift on the
        ``nodes`` map surfaces loudly (figmaclaw#11).
        """
        data = await self._get(
            f"/v1/files/{file_key}/nodes",
            params={"ids": page_node_id},
        )
        validated = _validate(
            NodesResponse, data,
            endpoint="GET /v1/files/{key}/nodes",
            context=f"file_key={file_key} ids={page_node_id}",
        )
        entry = validated.nodes.get(page_node_id)
        if entry is None or entry.document is None:
            return {}
        return entry.document

    async def get_file_full(
        self, file_key: str, *, version: str | None = None,
    ) -> dict[str, Any]:
        """GET /v1/files/{file_key}[?version=...] — full file tree.

        If *version* is given, fetches the file state at that historical version.
        Use for initial tracking or version diffs — one call returns all pages
        and all frames, which is far cheaper than per-page fetches.

        Returns the raw dict: the full recursive file tree is consumed by
        :func:`figmaclaw.commands.diff._extract_all_pages` which walks it
        directly, and typing the whole recursion is out of scope for
        figmaclaw#11. The top-level shape is the same as ``get_file_meta``
        but with ``depth=unlimited``, so a lightweight sanity check via
        :class:`FileMetaResponse` catches the common schema drift modes.
        """
        params = {"version": version} if version else None
        data = await self._get(f"/v1/files/{file_key}", params=params)
        # Sanity validation on the top-level envelope; recursive children stay raw.
        _validate(
            FileMetaResponse, data,
            endpoint="GET /v1/files/{key}" + (f"?version={version}" if version else ""),
            context=f"file_key={file_key}",
        )
        return data

    async def _get_url(self, url: str) -> dict[str, Any]:
        """GET an absolute Figma API URL (used for pagination)."""
        client = await self._ensure_client()
        full_url = url if url.startswith("http") else f"{self._base_url}{url}"
        last_exc: Exception | None = None
        for attempt in range(10):
            await self._pace()
            try:
                response = await client.get(full_url)
            except (httpx.RemoteProtocolError, httpx.ReadError,
                    httpx.ReadTimeout, httpx.ConnectError) as e:
                last_exc = e
                if attempt < 9:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise
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
        if last_exc:
            raise last_exc
        response.raise_for_status()
        return {}

    async def get_versions(
        self,
        file_key: str,
        *,
        max_pages: int = 20,
        stop_when: Callable[[VersionSummary], bool] | None = None,
    ) -> list[VersionSummary]:
        """GET /v1/files/{file_key}/versions — paginated version history.

        Returns typed :class:`VersionSummary` instances ordered
        newest-first. Follows pagination up to *max_pages* pages
        (default 20 pages × 50 per page = 1000 versions).

        If *stop_when* is provided, it should be a callable that takes a
        version and returns True when pagination should stop (e.g. when
        a version is older than a desired cutoff). The matching version
        and all newer ones are included in the result.

        Schema drift on individual version entries surfaces as a
        :class:`figma_api_models.FigmaAPIValidationError` at the first
        offending page — figmaclaw#11.
        """
        all_versions: list[VersionSummary] = []
        url: str | None = f"/v1/files/{file_key}/versions?page_size=50"
        for _ in range(max_pages):
            if not url:
                break
            data = await self._get_url(url)
            page = _validate(
                VersionsPage, data,
                endpoint="GET /v1/files/{key}/versions",
                context=f"file_key={file_key}",
            )
            if not page.versions:
                break
            all_versions.extend(page.versions)
            # Check if we've found a version older than the cutoff
            if stop_when and any(stop_when(v) for v in page.versions):
                break
            url = page.pagination.next_page if page.pagination else None
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

    async def list_team_projects(self, team_id: str) -> list[ProjectSummary]:
        """GET /v1/teams/{team_id}/projects — list projects for a team.

        Returns typed :class:`ProjectSummary` instances; callers access
        ``.id`` and ``.name`` via attribute access (figmaclaw#11).
        """
        data = await self._get(f"/v1/teams/{team_id}/projects")
        resp = _validate(
            TeamProjectsResponse, data,
            endpoint="GET /v1/teams/{team_id}/projects",
            context=f"team_id={team_id}",
        )
        return resp.projects

    async def list_project_files(self, project_id: str) -> list[FileSummary]:
        """GET /v1/projects/{project_id}/files — list files in a project.

        Returns typed :class:`FileSummary` instances; callers access
        ``.key``, ``.name``, ``.last_modified`` via attribute access
        (figmaclaw#11).
        """
        data = await self._get(f"/v1/projects/{project_id}/files")
        resp = _validate(
            ProjectFilesResponse, data,
            endpoint="GET /v1/projects/{project_id}/files",
            context=f"project_id={project_id}",
        )
        return resp.files

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
