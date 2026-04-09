"""Shared test fixtures and fake-data factories for figmaclaw tests.

Factories (plain functions, importable):
    fake_file_meta            — single-page FileMetaResponse
    fake_page_node            — minimal CANVAS with one SECTION+two FRAMEs
    fake_component_page_node  — CANVAS with a COMPONENT_SET section
    fake_page_node_with_children — CANVAS with a FRAME that has raw + instance children
    fake_get_nodes_response   — matching get_nodes reply for fake_page_node_with_children
    fake_file_meta_multi      — FileMetaResponse with N pages
    fake_page_node_for_id     — parameterised page node
    fake_file_meta_with_pages — FileMetaResponse with named pages
    make_pull_state           — FigmaSyncState with one tracked file

Fixtures:
    pull_env  — PullEnv(state, client, tmp_path) wired for a v1→v2 single-page pull.
                Override individual client methods for tests that need custom behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from figmaclaw.figma_api_models import FileMetaResponse
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_sync_state import FigmaSyncState


@dataclass
class PullEnv:
    state: FigmaSyncState
    client: MagicMock
    tmp_path: Path


def fake_file_meta(
    version: str = "v2",
    last_modified: str = "2026-03-31T12:00:00Z",
) -> FileMetaResponse:
    return FileMetaResponse.model_validate({
        "version": version,
        "lastModified": last_modified,
        "name": "Web App",
        "document": {
            "children": [
                {"id": "7741:45837", "name": "Onboarding", "type": "CANVAS"},
            ],
        },
    })


def fake_page_node(page_id: str = "7741:45837") -> dict:
    return {
        "id": page_id,
        "name": "Onboarding",
        "type": "CANVAS",
        "children": [
            {
                "id": "10:1",
                "name": "intro",
                "type": "SECTION",
                "children": [
                    {"id": "11:1", "name": "welcome", "type": "FRAME", "children": []},
                    {"id": "11:2", "name": "permissions", "type": "FRAME", "children": []},
                ],
            }
        ],
    }


def fake_component_page_node(page_id: str = "7741:45837") -> dict:
    """CANVAS page whose only section is a COMPONENT_SET-based component library."""
    return {
        "id": page_id,
        "name": "Components",
        "type": "CANVAS",
        "children": [
            {
                "id": "20:1",
                "name": "buttons",
                "type": "SECTION",
                "children": [
                    {"id": "30:1", "name": "Button / Primary", "type": "COMPONENT_SET", "children": []},
                    {"id": "30:2", "name": "Button / Secondary", "type": "COMPONENT_SET", "children": []},
                ],
            }
        ],
    }


def fake_page_node_with_children() -> dict:
    """Screen page whose frame has both raw (RECTANGLE, TEXT) and INSTANCE children."""
    return {
        "id": "7741:45837",
        "name": "Onboarding",
        "type": "CANVAS",
        "children": [
            {
                "id": "10:1",
                "name": "intro",
                "type": "SECTION",
                "children": [
                    {
                        "id": "11:1",
                        "name": "welcome",
                        "type": "FRAME",
                        "children": [],
                    },
                ],
            }
        ],
    }


def fake_get_nodes_response() -> dict:
    """Simulated get_nodes response: frame 11:1 has 1 INSTANCE + 2 raw children."""
    return {
        "11:1": {
            "id": "11:1",
            "type": "FRAME",
            "children": [
                {"type": "INSTANCE", "name": "AvatarV2"},
                {"type": "RECTANGLE", "name": "bg"},
                {"type": "TEXT", "name": "label"},
            ],
        }
    }


def fake_file_meta_multi(n_pages: int) -> FileMetaResponse:
    """FileMetaResponse with n_pages CANVAS children."""
    return FileMetaResponse.model_validate({
        "version": "v2",
        "lastModified": "2026-03-31T12:00:00Z",
        "name": "Web App",
        "document": {
            "children": [
                {"id": f"100:{i}", "name": f"Page {i}", "type": "CANVAS"}
                for i in range(1, n_pages + 1)
            ],
        },
    })


def fake_page_node_for_id(page_id: str, page_name: str) -> dict:
    return {
        "id": page_id,
        "name": page_name,
        "type": "CANVAS",
        "children": [
            {
                "id": f"s:{page_id}",
                "name": "section",
                "type": "SECTION",
                "children": [
                    {"id": f"f:{page_id}", "name": "frame", "type": "FRAME", "children": []},
                ],
            }
        ],
    }


def fake_file_meta_with_pages(*page_names: str) -> FileMetaResponse:
    return FileMetaResponse.model_validate({
        "version": "v2",
        "lastModified": "2026-03-31T12:00:00Z",
        "name": "Web App",
        "document": {
            "children": [
                {"id": f"100:{i}", "name": name, "type": "CANVAS"}
                for i, name in enumerate(page_names, 1)
            ],
        },
    })


def make_pull_state(
    tmp_path: Path,
    version: str = "v1",
    file_key: str = "abc123",
) -> FigmaSyncState:
    """Create a FigmaSyncState with a single tracked file at the given version."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(file_key, "Web App")
    state.manifest.files[file_key].version = version
    return state


@pytest.fixture
def pull_env(tmp_path: Path) -> PullEnv:
    """Standard single-page pull environment.

    Manifest: file_key="abc123", version="v1" (stale — triggers a pull).
    Figma: returns version "v2", single Onboarding page.
    Client methods are pre-wired with sensible defaults; override per test::

        def test_something(pull_env):
            pull_env.client.get_page = AsyncMock(return_value=fake_component_page_node())
            result = await pull_file(pull_env.client, "abc123", pull_env.state, pull_env.tmp_path)
    """
    state = make_pull_state(tmp_path)

    client = MagicMock(spec=FigmaClient)
    client.get_file_meta = AsyncMock(return_value=fake_file_meta())
    client.get_page = AsyncMock(return_value=fake_page_node())
    client.get_component_sets = AsyncMock(return_value=[])
    client.get_nodes = AsyncMock(return_value={})

    return PullEnv(state=state, client=client, tmp_path=tmp_path)
