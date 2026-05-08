from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from figmaclaw.instance_diff import (
    _metadata_for_node,
    diff_instance_against_master,
    diff_instances_against_masters,
    diff_nodes_against_master,
)
from figmaclaw.main import cli

CURRENT_HASH = "current-ds-hash"
OLD_HASH = "old-ds-hash"
COMPONENT_KEY = "component-published-key"
COMPONENT_SET_KEY = "component-set-published-key"


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
    assert diff.override_properties == []
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
    assert diff.override_properties == ["cornerRadius", "fills", "itemSpacing", "paddingLeft"]
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


def test_instance_diff_current_ds_can_match_file_or_published_key() -> None:
    by_file = diff_nodes_against_master(
        file_key="file123",
        instance_node=_matching_instance(),
        master_node=_master_node(),
        master_file_key="ds-file",
        master_node_id="99:1",
        master_library_hash=None,
        current_ds_library_hashes=set(),
        current_ds_file_keys={"ds-file"},
    )
    by_component_key = diff_nodes_against_master(
        file_key="file123",
        instance_node=_matching_instance(),
        master_node=_master_node(),
        master_file_key="remote-file",
        master_node_id="99:1",
        master_library_hash=None,
        master_component_key=COMPONENT_KEY,
        current_ds_library_hashes=set(),
        current_ds_published_keys={COMPONENT_KEY},
    )
    by_component_set_key = diff_nodes_against_master(
        file_key="file123",
        instance_node=_matching_instance(),
        master_node=_master_node(),
        master_file_key="remote-file",
        master_node_id="99:1",
        master_library_hash=None,
        master_component_set_key=COMPONENT_SET_KEY,
        current_ds_library_hashes=set(),
        current_ds_published_keys={COMPONENT_SET_KEY},
    )

    assert by_file.master.is_current_ds is True
    assert by_component_key.master.is_current_ds is True
    assert by_component_key.master.component_key == COMPONENT_KEY
    assert by_component_key.master.component_set_key is None
    assert by_component_set_key.master.is_current_ds is True
    assert by_component_set_key.master.component_set_key == COMPONENT_SET_KEY


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
    assert diff.override_properties == []


def test_instance_diff_local_draft_master_uses_overrides_as_structural_signal() -> None:
    diff = diff_nodes_against_master(
        file_key="audit-file",
        instance_node=_overridden_instance(),
        master_node=_master_node(),
        master_file_key="audit-file",
        master_node_id="99:1",
        master_library_hash=None,
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert diff.master.is_current_ds is False
    assert diff.master.library_hash is None
    assert diff.master.published_key is None
    assert diff.override_properties == ["cornerRadius", "fills", "itemSpacing", "paddingLeft"]


def test_instance_diff_override_kind_is_identity_triage_signal() -> None:
    master = _master_node()
    master["cornerRadius"] = 999
    master["fills"] = [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]
    master["itemSpacing"] = 8
    master["boundVariables"] = {
        "fills": [_var(CURRENT_HASH, "surface")],
        "itemSpacing": _var(CURRENT_HASH, "spacing-8"),
    }
    instance = _matching_instance()
    instance["cornerRadius"] = 6
    instance["fills"] = [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]
    instance["itemSpacing"] = 10
    instance["boundVariables"] = {
        "fills": [_var(OLD_HASH, "surface")],
        "itemSpacing": _var(OLD_HASH, "spacing-10"),
    }

    diff = diff_nodes_against_master(
        file_key="file123",
        instance_node=instance,
        master_node=master,
        master_file_key="ds-file",
        master_node_id="99:1",
        master_library_hash=CURRENT_HASH,
        current_ds_library_hashes={CURRENT_HASH},
    )

    by_property = {row.property: row for row in diff.properties}
    assert by_property["cornerRadius"].override_kind == "value"
    assert by_property["fills"].override_kind == "binding"
    assert by_property["itemSpacing"].override_kind == "both"


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
        side_effect=[
            {
                "nodes": {"10:2": {"document": instance}},
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "componentSetKey": COMPONENT_SET_KEY,
                        "file_key": "ds-file",
                        "node_id": "99:1",
                        "library_hash": CURRENT_HASH,
                    }
                },
            },
            {
                "nodes": {"99:1": {"document": master}},
                "components": {},
                "componentSets": {},
            },
        ]
    )
    client.get_nodes = AsyncMock(return_value={"99:1": master})
    client.get_component_set = AsyncMock(return_value={})

    diff = await diff_instance_against_master(
        client,
        "file123",
        "10:2",
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert diff.master.file_key == "ds-file"
    assert diff.master.published_key == COMPONENT_SET_KEY
    assert diff.master.component_key == COMPONENT_KEY
    assert diff.master.component_set_key == COMPONENT_SET_KEY
    assert diff.master.library_hash == CURRENT_HASH
    assert diff.master.is_current_ds is True
    assert diff.master.is_resolvable is True
    assert client.get_nodes_response.await_args_list[1].args == ("ds-file", ["99:1"])
    assert client.get_nodes_response.await_args_list[1].kwargs == {"depth": 1}


@pytest.mark.asyncio
async def test_diff_instance_against_master_marks_fetch_failure_unresolvable() -> None:
    instance = _matching_instance()
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        return_value={
            "nodes": {"10:2": {"document": instance}},
            "components": {
                "99:1": {
                    "key": COMPONENT_KEY,
                    "file_key": "ds-file",
                    "node_id": "99:1",
                    "library_hash": CURRENT_HASH,
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
        side_effect=[
            {
                "nodes": {"10:2": {"document": _overridden_instance()}},
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "file_key": "ds-file",
                        "node_id": "99:1",
                        "library_hash": CURRENT_HASH,
                    }
                },
            },
            {
                "nodes": {"99:1": {"document": _master_node()}},
                "components": {},
                "componentSets": {},
            },
        ]
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
    assert data["override_properties"] == [
        "cornerRadius",
        "fills",
        "itemSpacing",
        "paddingLeft",
    ]
    assert {row["property"] for row in data["properties"] if row["is_override"]} >= {
        "cornerRadius",
        "fills",
        "itemSpacing",
        "paddingLeft",
    }


@pytest.mark.asyncio
async def test_diff_instance_against_master_uses_component_key_for_remote_master() -> None:
    instance = _matching_instance()
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        return_value={
            "nodes": {"10:2": {"document": instance}},
            "components": {
                "99:1": {
                    "key": COMPONENT_KEY,
                    "componentSetKey": COMPONENT_SET_KEY,
                }
            },
        }
    )
    client.get_component = AsyncMock(
        return_value={
            "file_key": "ds-file",
            "node_id": "99:1",
        }
    )
    client.get_component_set = AsyncMock(
        return_value={
            "componentPropertyDefinitions": {
                "size": {"type": "VARIANT", "variantOptions": ["sm", "lg"]}
            }
        }
    )
    client.get_nodes = AsyncMock(return_value={"99:1": _master_node()})

    diff = await diff_instance_against_master(
        client,
        "file123",
        "10:2",
        current_ds_library_hashes=set(),
        current_ds_file_keys={"ds-file"},
    )

    client.get_component.assert_awaited_once_with(COMPONENT_KEY)
    client.get_component_set.assert_awaited_once_with(COMPONENT_SET_KEY)
    assert diff.master.published_key == COMPONENT_SET_KEY
    assert diff.master.is_current_ds is True
    assert diff.variant.available[0]["property"] == "size"


@pytest.mark.asyncio
async def test_diff_instance_against_master_reads_master_components_and_component_sets() -> None:
    instance = _matching_instance()
    instance["componentId"] = "99:1"
    master = _master_node()
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        side_effect=[
            {
                "nodes": {"10:2": {"document": instance}},
            },
            {
                "nodes": {
                    "99:1": {
                        "document": master,
                        "components": {
                            "99:1": {
                                "key": COMPONENT_KEY,
                                "name": "color=primary, size=lg",
                                "remote": True,
                                "componentSetId": "88:1",
                            }
                        },
                        "componentSets": {
                            "88:1": {
                                "key": COMPONENT_SET_KEY,
                                "name": "button",
                                "remote": True,
                            }
                        },
                    }
                }
            },
        ]
    )
    client.get_nodes = AsyncMock(return_value={})

    diff = await diff_instance_against_master(
        client,
        "file123",
        "10:2",
        current_ds_library_hashes={COMPONENT_SET_KEY},
    )

    assert diff.master.component_key == COMPONENT_KEY
    assert diff.master.component_set_key == COMPONENT_SET_KEY
    assert diff.master.published_key == COMPONENT_SET_KEY
    assert diff.master.is_current_ds is True


def test_metadata_for_node_reads_figma_nested_nodes_envelope() -> None:
    payload = {
        "nodes": {
            "9451:314": {
                "document": {"id": "9451:314", "type": "INSTANCE"},
                "components": {
                    "9465:2130": {
                        "key": COMPONENT_KEY,
                        "remote": True,
                        "componentSetId": "9465:2033",
                    }
                },
                "componentSets": {
                    "9465:2033": {
                        "key": COMPONENT_SET_KEY,
                        "remote": True,
                    }
                },
            }
        }
    }

    component = _metadata_for_node(payload, "components", "9465:2130")
    component_set = _metadata_for_node(payload, "componentSets", "9465:2033")

    assert component["key"] == COMPONENT_KEY
    assert component_set["key"] == COMPONENT_SET_KEY


@pytest.mark.asyncio
async def test_diff_instances_against_masters_batches_instance_and_master_fetches() -> None:
    first = _overridden_instance()
    first["id"] = "10:2"
    second = _matching_instance()
    second["id"] = "10:3"
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        side_effect=[
            {
                "nodes": {
                    "10:2": {"document": first},
                    "10:3": {"document": second},
                },
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "file_key": "ds-file",
                        "node_id": "99:1",
                        "library_hash": CURRENT_HASH,
                    }
                },
            },
            {
                "nodes": {"99:1": {"document": _master_node()}},
            },
        ]
    )

    diffs = await diff_instances_against_masters(
        client,
        "file123",
        ["10:2", "10:3"],
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert [diff.instance.node_id for diff in diffs] == ["10:2", "10:3"]
    assert diffs[0].model_dump()["override_properties"] == [
        "cornerRadius",
        "fills",
        "itemSpacing",
        "paddingLeft",
    ]
    assert diffs[1].model_dump()["override_properties"] == []
    assert client.get_nodes_response.await_args_list[0].args == (
        "file123",
        ["10:2", "10:3"],
    )
    assert client.get_nodes_response.await_args_list[1].args == ("ds-file", ["99:1"])


@pytest.mark.asyncio
async def test_diff_instances_against_masters_chunks_instance_fetches() -> None:
    instance_ids = [f"10:{index}" for index in range(51)]
    instance_nodes = {}
    for node_id in instance_ids:
        node = _matching_instance()
        node["id"] = node_id
        instance_nodes[node_id] = {"document": node}
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        side_effect=[
            {
                "nodes": dict(list(instance_nodes.items())[:50]),
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "file_key": "ds-file",
                        "node_id": "99:1",
                        "library_hash": CURRENT_HASH,
                    }
                },
            },
            {
                "nodes": dict(list(instance_nodes.items())[50:]),
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "file_key": "ds-file",
                        "node_id": "99:1",
                        "library_hash": CURRENT_HASH,
                    }
                },
            },
            {
                "nodes": {"99:1": {"document": _master_node()}},
            },
        ]
    )

    diffs = await diff_instances_against_masters(
        client,
        "file123",
        instance_ids,
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert len(diffs) == 51
    assert client.get_nodes_response.await_args_list[0].args == ("file123", instance_ids[:50])
    assert client.get_nodes_response.await_args_list[1].args == ("file123", instance_ids[50:])
    assert client.get_nodes_response.await_args_list[2].args == ("ds-file", ["99:1"])


@pytest.mark.asyncio
async def test_diff_instances_against_masters_emits_error_for_missing_node() -> None:
    first = _overridden_instance()
    first["id"] = "10:2"
    second = _matching_instance()
    second["id"] = "10:3"
    client = MagicMock()
    client.get_nodes_response = AsyncMock(
        side_effect=[
            {
                "nodes": {
                    "10:2": {"document": first},
                    "10:3": {"document": second},
                },
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "file_key": "ds-file",
                        "node_id": "99:1",
                        "library_hash": CURRENT_HASH,
                    }
                },
            },
            {
                "nodes": {"99:1": {"document": _master_node()}},
            },
        ]
    )

    records = await diff_instances_against_masters(
        client,
        "file123",
        ["10:2", "10:404", "10:3"],
        current_ds_library_hashes={CURRENT_HASH},
    )

    assert [record.instance.node_id for record in records] == ["10:2", "10:404", "10:3"]
    assert records[0].model_dump()["override_properties"]
    assert records[1].model_dump() == {
        "instance": {"file_key": "file123", "node_id": "10:404"},
        "error": "10:404: node not found in Figma response",
        "is_resolvable": False,
    }
    assert records[2].model_dump()["override_properties"] == []


def test_inspect_instance_cli_reports_usage_error_for_non_instance(tmp_path, monkeypatch) -> None:
    fake = MagicMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    fake.get_nodes_response = AsyncMock(
        return_value={
            "nodes": {"10:2": {"document": {"id": "10:2", "type": "FRAME"}}},
            "components": {},
        }
    )
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
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 2
    assert "expected INSTANCE, got FRAME" in result.output


def test_inspect_instance_cli_accepts_current_ds_file_key(tmp_path, monkeypatch) -> None:
    fake = MagicMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    fake.get_component_sets = AsyncMock(return_value=[{"key": COMPONENT_SET_KEY}])
    fake.get_component = AsyncMock(return_value={"file_key": "remote-ds-file", "node_id": "99:1"})
    fake.get_component_set = AsyncMock(return_value={})
    fake.get_nodes_response = AsyncMock(
        side_effect=[
            {
                "nodes": {"10:2": {"document": _matching_instance()}},
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "componentSetKey": COMPONENT_SET_KEY,
                    }
                },
            },
            {
                "nodes": {"99:1": {"document": _master_node()}},
            },
        ]
    )
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
                "dcDETwKMNGpK39FfApg7Ki",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert json.loads(result.output)["master"]["is_current_ds"] is True
    fake.get_component_sets.assert_awaited_once_with("dcDETwKMNGpK39FfApg7Ki")


def test_inspect_instance_cli_outputs_jsonl_for_nodes_from_file(tmp_path, monkeypatch) -> None:
    nodes_path = tmp_path / "audit_nodes.jsonl"
    nodes_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "10:2", "type": "INSTANCE"}),
                json.dumps({"id": "I9451:314;148:427", "type": "INSTANCE"}),
                json.dumps({"id": "10:404", "type": "INSTANCE"}),
                json.dumps({"id": "10:99", "type": "FRAME"}),
                json.dumps({"id": "10:3", "type": "INSTANCE"}),
            ]
        ),
        encoding="utf-8",
    )
    first = _overridden_instance()
    first["id"] = "10:2"
    second = _matching_instance()
    second["id"] = "10:3"
    fake = MagicMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    fake.get_nodes_response = AsyncMock(
        side_effect=[
            {
                "nodes": {
                    "10:2": {"document": first},
                    "10:3": {"document": second},
                },
                "components": {
                    "99:1": {
                        "key": COMPONENT_KEY,
                        "file_key": "ds-file",
                        "node_id": "99:1",
                        "library_hash": CURRENT_HASH,
                    }
                },
            },
            {
                "nodes": {"99:1": {"document": _master_node()}},
            },
        ]
    )
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
                "--nodes-from",
                str(nodes_path),
                "--filter",
                "type=INSTANCE",
                "--current-ds-hash",
                CURRENT_HASH,
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    rows = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert [row["instance"]["node_id"] for row in rows] == ["10:2", "10:404", "10:3"]
    assert rows[0]["override_properties"]
    assert rows[1] == {
        "error": "10:404: node not found in Figma response",
        "instance": {"file_key": "file123", "node_id": "10:404"},
        "is_resolvable": False,
    }
    assert rows[2]["override_properties"] == []
    assert "skipped 1 synthesized nested instance ids" in result.output
    assert fake.get_nodes_response.await_args_list[0].args == (
        "file123",
        ["10:2", "10:404", "10:3"],
    )
