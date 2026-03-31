"""Async Figma REST API client."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class FigmaClient:
    """Async client for the Figma REST API.

    Uses X-Figma-Token header (not Authorization: Bearer).
    Retries on 429 (rate limit) and 5xx errors with exponential backoff.
    """

    _base_url = "https://api.figma.com"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None

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

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET request with retry on 429 / 5xx."""
        client = await self._ensure_client()
        url = f"{self._base_url}{path}"
        for attempt in range(5):
            response = await client.get(url, params=params)
            if response.status_code == 429:
                retry_after = int(response.headers.get("retry-after", "5"))
                await asyncio.sleep(max(retry_after, 1))
                continue
            if response.status_code >= 500 and attempt < 4:
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

    async def get_file_full(self, file_key: str) -> dict[str, Any]:
        """GET /v1/files/{file_key} — full file tree. Use only for initial track."""
        return await self._get(f"/v1/files/{file_key}")

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
