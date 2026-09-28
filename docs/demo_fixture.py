#!/usr/bin/env python3
"""Mirror a bundled Figma fixture into local markdown.

Used by docs/demo.tape. No Figma credentials and no network access: the
script drives the real ``pull_file`` path with an in-process fixture client.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from figmaclaw.figma_api_models import FileMetaResponse
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.pull_logic import pull_file

FILE_KEY = "demo01"
FILE_NAME = "Shop"
PAGE_ID = "12:40"
PAGE_NAME = "Checkout"


def file_meta() -> FileMetaResponse:
    """Return the fixture file listing (one Checkout page)."""
    return FileMetaResponse.model_validate(
        {
            "name": FILE_NAME,
            "version": "1",
            "lastModified": "2026-09-27T00:00:00Z",
            "document": {
                "id": "0:0",
                "name": "Document",
                "type": "DOCUMENT",
                "children": [{"id": PAGE_ID, "name": PAGE_NAME, "type": "CANVAS"}],
            },
        }
    )


def _navigate_to(*destination_ids: str) -> list[dict[str, Any]]:
    """Prototype reactions, as the Figma REST API returns them for a click-through link."""
    return [
        {"trigger": {"type": "ON_CLICK"}, "action": {"navigation": "NAVIGATE", "destinationId": d}}
        for d in destination_ids
    ]


def _frame(node_id: str, name: str, *destination_ids: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": name,
        "type": "FRAME",
        "children": [],
        "reactions": _navigate_to(*destination_ids),
    }


def page_node() -> dict[str, Any]:
    """Return a Checkout canvas: two sections and a clickable prototype flow."""
    return {
        "id": PAGE_ID,
        "name": PAGE_NAME,
        "type": "CANVAS",
        "children": [
            {
                "id": "20:1",
                "name": "Purchase",
                "type": "SECTION",
                "children": [
                    _frame("21:1", "Cart", "21:2"),
                    _frame("21:2", "Checkout", "21:3", "22:1"),
                    _frame("21:3", "Order confirmation"),
                ],
            },
            {
                "id": "20:2",
                "name": "Errors",
                "type": "SECTION",
                "children": [_frame("22:1", "Payment declined", "21:2")],
            },
        ],
    }


class FixtureClient:
    """Stand-in for ``FigmaClient`` that serves the bundled Checkout fixture."""

    async def get_file_meta(self, file_key: str) -> FileMetaResponse:
        _ = file_key
        return file_meta()

    async def get_pages(
        self,
        file_key: str,
        page_node_ids: list[str],
        *,
        version: str | None = None,
        batch_size: int = 10,
    ) -> dict[str, dict[str, Any]]:
        _ = (file_key, version, batch_size)
        node = page_node()
        return {page_id: node for page_id in page_node_ids}

    async def get_page(self, file_key: str, page_node_id: str) -> dict[str, Any]:
        _ = (file_key, page_node_id)
        return page_node()

    async def get_component_sets(self, file_key: str) -> list[dict[str, Any]]:
        _ = file_key
        return []

    async def get_nodes(
        self,
        file_key: str,
        node_ids: list[str],
        *,
        depth: int | None = 1,
        geometry: str | None = None,
    ) -> dict[str, Any]:
        _ = (file_key, node_ids, depth, geometry)
        return {}


async def mirror(repo_dir: Path) -> int:
    """Track the fixture file and pull it into ``repo_dir``."""
    state = FigmaSyncState(repo_dir)
    state.load()
    state.add_tracked_file(FILE_KEY, FILE_NAME)
    state.save()

    result = await pull_file(
        FixtureClient(),  # type: ignore[arg-type]
        FILE_KEY,
        state,
        repo_dir,
        force=True,
    )
    state.save()

    print(f"Tracking {FILE_NAME!r} ({FILE_KEY})")
    print(f"Wrote {result.pages_written} page(s)")
    for path in result.md_paths:
        print(f"  → {path}")
    if result.pages_written == 0 or result.pages_errored:
        print(
            f"fixture pull failed: written={result.pages_written} errored={result.pages_errored}",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(),
        help="Consumer repo to write figma/ markdown into (default: cwd).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(mirror(args.repo))


if __name__ == "__main__":
    raise SystemExit(main())
