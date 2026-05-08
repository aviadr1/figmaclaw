from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from figmaclaw.instance_diff import (
    diff_instance_against_master,
    diff_nodes_against_master,
)
from figmaclaw.main import cli

CURRENT_HASH = "current-ds-hash"
OLD_HASH = "old-ds-hash"


def _var(library_hash: str, name: str) -> dict:
    return {"id": f"VariableID:{library_hash}/{name}", "name": name}


def _master_node(*, library_hash: str = CURRENT_HASH) -> dict:
    return {
        "id": "99:1",
        "name": "Button/lg",
        "type": "COMPONENT",
        "cornerRadius": 999,
        "paddingLeft": 16,
        "itemSpacing": 8,
        "fills": [{"type": "SOLID", "color": {"r": 0, "g": 0.1, "b": 1}}],
        "boundVariables": {
            "cornerRadius": _var(library_hash, "radius-rounded"),
            "paddingLeft": _var(library_hash, "spacing-16"),
            "itemSpacing": _var(library_hash, "spacing-8"),
            "fills": [_var(library_hash, "bg-brand-strong")],
        },
    }


def _matching_instance() -> dict:
    node = _master_node()
    node.update(
        {"id": "10:2", "name": "Button instance", "type": "INSTANCE", "componentId": "99:1"}
    )
    return node


def _overridden_instance() -> dict:
    node = _matching_instance()
    node["cornerRadius"] = 6
    node["paddingLeft"] = 24
    node["itemSpacing"] = 16
    node["fills"] = [{"type": "SOLID", "color": {"r": 0.7, "g": 0, "b": 0.4}}]
    node["boundVariables"] = {
        "cornerRadius": _var(CURRENT_HASH, "radius-sm"),
        "paddingLeft": _var(CURRENT_HASH, "spacing-24"),
        "itemSpacing": _var(CURRENT_HASH, "spacing-lg"),
        "fills": [_var(CURRENT_HASH, "fg-brand")],
    }
    return node


def test_instance_diff_same_ds_no_override() -> None:
    diff = diff_nodes_against_master(
        file_key="file123",
        instance_node=_matching_instance(),
        master_node=_master_node(),
        master_file_key="ds-file",
        master_node_id="99:1",
        master_library_hash=CURRENT_HASH,
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert diff.master.is_current_ds is True
    assert diff.master.is_resolvable is True
    assert all(row.override_kind == "none" for row in diff.properties)


def test_instance_diff_same_ds_with_overrides() -> None:
    diff = diff_nodes_against_master(
        file_key="file123",
        instance_node=_overridden_instance(),
        master_node=_master_node(),
        master_file_key="ds-file",
        master_node_id="99:1",
        master_library_hash=CURRENT_HASH,
        current_ds_library_hashes={CURRENT_HASH},
    )

    by_property = {row.property: row for row in diff.properties}
    assert diff.master.is_current_ds is True
    assert by_property["cornerRadius"].override_kind == "both"
    assert by_property["fills"].override_kind == "both"
    assert by_property["paddingLeft"].override_kind == "both"
    assert by_property["itemSpacing"].override_kind == "both"


def test_instance_diff_old_ds_master_classification() -> None:
    diff = diff_nodes_against_master(
        file_key="file123",
        instance_node=_matching_instance(),
        master_node=_master_node(library_hash=OLD_HASH),
        master_file_key="old-file",
        master_node_id="99:1",
        master_library_hash=OLD_HASH,
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert diff.master.library_hash == OLD_HASH
    assert diff.master.is_current_ds is False


def test_instance_diff_unresolvable_master() -> None:
    diff = diff_nodes_against_master(
        file_key="file123",
        instance_node=_matching_instance(),
        master_node=None,
        master_file_key="missing-file",
        master_node_id="99:1",
        master_library_hash=CURRENT_HASH,
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert diff.master.is_current_ds is True
    assert diff.master.is_resolvable is False
    assert diff.properties == []


def test_instance_diff_variant_set_metadata() -> None:
    instance = _matching_instance()
    instance["componentProperties"] = {
        "size": {"type": "VARIANT", "value": "lg"},
        "state": {"type": "VARIANT", "value": "default"},
    }
    master = _master_node()
    master["componentSetId"] = "88:1"
    component_set = {
        "id": "88:1",
        "type": "COMPONENT_SET",
        "componentPropertyDefinitions": {
            "size": {"type": "VARIANT", "variantOptions": ["sm", "lg"]},
            "state": {"type": "VARIANT", "variantOptions": ["default", "hover"]},
        },
    }

    diff = diff_nodes_against_master(
        file_key="file123",
        instance_node=instance,
        master_node=master,
        master_file_key="ds-file",
        master_node_id="99:1",
        master_library_hash=CURRENT_HASH,
        current_ds_library_hashes={CURRENT_HASH},
        component_set_node=component_set,
    )

    assert diff.variant.selected == {"size": "lg", "state": "default"}
    assert {row["property"] for row in diff.variant.available} == {"size", "state"}


@pytest.mark.asyncio
async def test_diff_instance_against_master_resolves_from_component_metadata() -> None:
    instance = _overridden_instance()
    master = _master_node()
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        return_value={
            "nodes": {"10:2": {"document": instance}},
            "components": {
                "99:1": {
                    "key": CURRENT_HASH,
                    "file_key": "ds-file",
                    "node_id": "99:1",
                }
            },
        }
    )
    client.get_nodes = AsyncMock(return_value={"99:1": master})

    diff = await diff_instance_against_master(
        client,
        "file123",
        "10:2",
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert diff.master.file_key == "ds-file"
    assert diff.master.published_key == CURRENT_HASH
    assert diff.master.is_current_ds is True
    assert diff.master.is_resolvable is True
    client.get_nodes.assert_awaited_once_with("ds-file", ["99:1"], depth=1)


@pytest.mark.asyncio
async def test_diff_instance_against_master_marks_fetch_failure_unresolvable() -> None:
    instance = _matching_instance()
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        return_value={
            "nodes": {"10:2": {"document": instance}},
            "components": {
                "99:1": {
                    "key": CURRENT_HASH,
                    "file_key": "ds-file",
                    "node_id": "99:1",
                }
            },
        }
    )
    request = httpx.Request("GET", "https://api.figma.com/v1/files/ds-file/nodes")
    response = httpx.Response(404, request=request)
    client.get_nodes = AsyncMock(
        side_effect=httpx.HTTPStatusError("missing", request=request, response=response)
    )

    diff = await diff_instance_against_master(
        client,
        "file123",
        "10:2",
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert diff.master.is_resolvable is False
    assert diff.properties == []


def test_inspect_instance_cli_prints_instance_diff_json(tmp_path, monkeypatch) -> None:
    fake = MagicMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    fake.get_nodes_response = AsyncMock(
        return_value={
            "nodes": {"10:2": {"document": _overridden_instance()}},
            "components": {
                "99:1": {
                    "key": CURRENT_HASH,
                    "file_key": "ds-file",
                    "node_id": "99:1",
                }
            },
        }
    )
    fake.get_nodes = AsyncMock(return_value={"99:1": _master_node()})
    monkeypatch.setenv("FIGMA_API_KEY", "figd_test")

    with patch("figmaclaw.commands.inspect_instance.FigmaClient", return_value=fake):
        result = CliRunner().invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "inspect-instance",
                "--file-key",
                "file123",
                "--node",
                "10:2",
                "--current-ds-hash",
                CURRENT_HASH,
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["instance"] == {"file_key": "file123", "node_id": "10:2"}
    assert data["master"]["is_current_ds"] is True
    assert {row["property"] for row in data["properties"] if row["is_override"]} >= {
        "cornerRadius",
        "fills",
        "itemSpacing",
        "paddingLeft",
    }
