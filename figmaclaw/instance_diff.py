"""Instance/master property diff primitives."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from figmaclaw.figma_client import FigmaClient, normalize_node_id
from figmaclaw.token_scan import bound_variable_id, paint_bound_variable_id, variable_library_hash

OverrideKind = Literal["none", "value", "binding", "both"]
NODE_FETCH_BATCH_SIZE = 50

SCALAR_PROPERTIES = (
    "cornerRadius",
    "strokeWeight",
    "paddingLeft",
    "paddingRight",
    "paddingTop",
    "paddingBottom",
    "itemSpacing",
    "opacity",
)
CORNER_RADIUS_PROPERTIES = (
    "topLeftRadius",
    "topRightRadius",
    "bottomRightRadius",
    "bottomLeftRadius",
)
PAINT_PROPERTIES = ("fills", "strokes")
TEXT_STYLE_PROPERTIES = (
    "fontFamily",
    "fontStyle",
    "fontWeight",
    "fontSize",
    "lineHeight",
    "letterSpacing",
)
LIST_PROPERTIES = ("effects",)


class InstanceRef(BaseModel):
    """Identity of the inspected instance."""

    file_key: str
    node_id: str


class MasterRef(BaseModel):
    """Best-effort identity of the resolved master component."""

    file_key: str | None = None
    node_id: str | None = None
    library_hash: str | None = None
    published_key: str | None = None
    component_key: str | None = None
    component_set_key: str | None = None
    is_current_ds: bool = False
    is_resolvable: bool = False


class VariantInfo(BaseModel):
    """Selected and available variant/component-property metadata."""

    selected: dict[str, Any] = Field(default_factory=dict)
    available: list[dict[str, Any]] = Field(default_factory=list)


class PropertyDiff(BaseModel):
    """Resolved comparison for one supported property."""

    property: str
    master_value: Any = None
    master_binding: dict[str, Any] | None = None
    instance_value: Any = None
    instance_binding: dict[str, Any] | None = None
    is_override: bool
    override_kind: OverrideKind


class InstanceDiff(BaseModel):
    """JSON-serializable instance/master diff."""

    instance: InstanceRef
    master: MasterRef
    variant: VariantInfo = Field(default_factory=VariantInfo)
    properties: list[PropertyDiff] = Field(default_factory=list)
    override_properties: list[str] = Field(default_factory=list)


class InstanceDiffError(BaseModel):
    """JSON-serializable per-record failure for batch inspection."""

    instance: InstanceRef
    error: str
    is_resolvable: Literal[False] = False


class NodeNotFoundError(ValueError):
    """Raised when Figma omits a requested node from a successful /nodes response."""


async def diff_instance_against_master(
    client: FigmaClient,
    file_key: str,
    instance_node_id: str,
    *,
    current_ds_library_hashes: set[str],
    current_ds_file_keys: set[str] | None = None,
    current_ds_published_keys: set[str] | None = None,
) -> InstanceDiff:
    """Fetch an instance and compare supported properties against its master."""
    normalized_instance_id = normalize_node_id(instance_node_id)
    instance_payload = await client.get_nodes_response(file_key, [instance_node_id], depth=1)
    instance_node = _node_from_response(instance_payload, normalized_instance_id)
    if instance_node.get("type") != "INSTANCE":
        raise ValueError(f"{instance_node_id}: expected INSTANCE, got {instance_node.get('type')}")

    component_meta = _component_meta_for_instance(instance_payload, instance_node)
    component_set_meta = _component_set_meta_for_component(instance_payload, component_meta)
    master_ref = await _resolve_master_ref(
        client,
        file_key=file_key,
        instance_node=instance_node,
        component_meta=component_meta,
        component_set_meta=component_set_meta,
        current_ds_library_hashes=current_ds_library_hashes,
        current_ds_file_keys=current_ds_file_keys or set(),
        current_ds_published_keys=current_ds_published_keys or set(),
    )

    master_node: dict[str, Any] | None = None
    component_set_node: dict[str, Any] | None = None
    if master_ref.file_key and master_ref.node_id:
        master_payload = await _fetch_optional_node_response(
            client,
            master_ref.file_key,
            master_ref.node_id,
        )
        master_node = _optional_node_from_response(master_payload, master_ref.node_id)
        master_ref.is_resolvable = master_node is not None
        master_component_meta = _metadata_for_node(master_payload, "components", master_ref.node_id)
        master_component_set_meta = _component_set_meta_for_component(
            master_payload,
            master_component_meta,
        )
        _update_master_ref_from_metadata(
            master_ref,
            component_meta=master_component_meta,
            component_set_meta=master_component_set_meta,
            current_ds_library_hashes=current_ds_library_hashes,
            current_ds_file_keys=current_ds_file_keys or set(),
            current_ds_published_keys=current_ds_published_keys or set(),
        )
        component_set_id = _component_set_id(master_component_meta or component_meta, master_node)
        if component_set_id and master_ref.file_key:
            component_set_node = await _fetch_optional_node(
                client, master_ref.file_key, component_set_id
            )
        if component_set_node is None and master_component_set_meta:
            component_set_node = master_component_set_meta
        if component_set_node is None and master_ref.component_set_key:
            component_set_meta = await _fetch_component_set_meta(
                client,
                master_ref.component_set_key,
            )
            component_set_file_key = _first_str(component_set_meta, "file_key", "fileKey")
            component_set_node_id = _first_str(component_set_meta, "node_id", "nodeId")
            if component_set_file_key and component_set_node_id:
                component_set_node = await _fetch_optional_node(
                    client,
                    component_set_file_key,
                    component_set_node_id,
                )
            elif component_set_meta:
                component_set_node = component_set_meta

    return _build_instance_diff(
        instance=InstanceRef(file_key=file_key, node_id=normalized_instance_id),
        master=master_ref,
        instance_node=instance_node,
        master_node=master_node,
        component_set_node=component_set_node,
    )


async def diff_instances_against_masters(
    client: FigmaClient,
    file_key: str,
    instance_node_ids: list[str],
    *,
    current_ds_library_hashes: set[str],
    current_ds_file_keys: set[str] | None = None,
    current_ds_published_keys: set[str] | None = None,
) -> list[InstanceDiff | InstanceDiffError]:
    """Fetch instances in one batch and compare them against their masters.

    The CLI uses this for audit/debugger bulk reads. It intentionally keeps the
    same output model as the single-instance path while avoiding one REST
    instance fetch and one REST master fetch per row.
    """
    normalized_ids = [normalize_node_id(node_id) for node_id in instance_node_ids]
    if not normalized_ids:
        return []

    current_ds_file_keys = current_ds_file_keys or set()
    current_ds_published_keys = current_ds_published_keys or set()
    instance_payload = await _fetch_nodes_response_chunks(
        client,
        file_key,
        instance_node_ids,
    )
    component_cache: dict[str, dict[str, Any]] = {}
    component_set_cache: dict[str, dict[str, Any]] = {}
    records: list[InstanceDiff | InstanceDiffError | None] = []
    rows: list[tuple[int, dict[str, Any], dict[str, Any], MasterRef]] = []
    master_ids_by_file: dict[str, list[str]] = defaultdict(list)

    for original_id, normalized_id in zip(instance_node_ids, normalized_ids, strict=True):
        try:
            instance_node = _node_from_response(instance_payload, normalized_id)
        except NodeNotFoundError as exc:
            records.append(
                InstanceDiffError(
                    instance=InstanceRef(file_key=file_key, node_id=normalized_id),
                    error=str(exc),
                )
            )
            continue
        if instance_node.get("type") != "INSTANCE":
            raise ValueError(f"{original_id}: expected INSTANCE, got {instance_node.get('type')}")

        component_meta = _component_meta_for_instance(instance_payload, instance_node)
        component_set_meta = _component_set_meta_for_component(instance_payload, component_meta)
        master_ref = await _resolve_master_ref_cached(
            client,
            file_key=file_key,
            instance_node=instance_node,
            component_meta=component_meta,
            component_set_meta=component_set_meta,
            current_ds_library_hashes=current_ds_library_hashes,
            current_ds_file_keys=current_ds_file_keys,
            current_ds_published_keys=current_ds_published_keys,
            component_cache=component_cache,
        )
        row_index = len(records)
        records.append(None)
        rows.append((row_index, instance_node, component_meta, master_ref))
        if master_ref.file_key and master_ref.node_id:
            master_ids_by_file[master_ref.file_key].append(master_ref.node_id)

    master_payloads: dict[str, dict[str, Any]] = {}
    for master_file_key, master_node_ids in master_ids_by_file.items():
        master_payloads[master_file_key] = await _fetch_optional_nodes_response(
            client,
            master_file_key,
            list(dict.fromkeys(master_node_ids)),
        )

    resolved_rows: list[
        tuple[
            int,
            dict[str, Any],
            MasterRef,
            dict[str, Any] | None,
            dict[str, Any],
            str | None,
        ]
    ] = []
    component_set_ids_by_file: dict[str, list[str]] = defaultdict(list)
    for row_index, instance_node, component_meta, master_ref in rows:
        master_payload = master_payloads.get(master_ref.file_key or "", {})
        master_node = None
        if master_ref.node_id:
            master_node = _optional_node_from_response(master_payload, master_ref.node_id)
        master_ref.is_resolvable = master_node is not None
        master_component_meta = (
            _metadata_for_node(master_payload, "components", master_ref.node_id)
            if master_ref.node_id
            else {}
        )
        master_component_set_meta = _component_set_meta_for_component(
            master_payload,
            master_component_meta,
        )
        _update_master_ref_from_metadata(
            master_ref,
            component_meta=master_component_meta,
            component_set_meta=master_component_set_meta,
            current_ds_library_hashes=current_ds_library_hashes,
            current_ds_file_keys=current_ds_file_keys,
            current_ds_published_keys=current_ds_published_keys,
        )
        component_set_id = _component_set_id(master_component_meta or component_meta, master_node)
        if component_set_id and master_ref.file_key:
            component_set_ids_by_file[master_ref.file_key].append(component_set_id)
        resolved_rows.append(
            (
                row_index,
                instance_node,
                master_ref,
                master_node,
                master_component_set_meta,
                component_set_id,
            )
        )

    component_set_payloads: dict[str, dict[str, Any]] = {}
    for component_set_file_key, component_set_ids in component_set_ids_by_file.items():
        component_set_payloads[component_set_file_key] = await _fetch_optional_nodes_response(
            client,
            component_set_file_key,
            list(dict.fromkeys(component_set_ids)),
        )

    for (
        row_index,
        instance_node,
        master_ref,
        master_node,
        master_component_set_meta,
        component_set_id,
    ) in resolved_rows:
        component_set_node = None
        if component_set_id and master_ref.file_key:
            component_set_node = _optional_node_from_response(
                component_set_payloads.get(master_ref.file_key, {}),
                component_set_id,
            )
        if component_set_node is None and master_component_set_meta:
            component_set_node = master_component_set_meta
        if component_set_node is None and master_ref.component_set_key:
            component_set_node = await _fetch_component_set_meta_cached(
                client,
                master_ref.component_set_key,
                component_set_cache,
            )
        records[row_index] = _build_instance_diff(
            instance=InstanceRef(
                file_key=file_key,
                node_id=normalize_node_id(str(instance_node.get("id") or "")),
            ),
            master=master_ref,
            instance_node=instance_node,
            master_node=master_node,
            component_set_node=component_set_node,
        )
    return [record for record in records if record is not None]


def diff_nodes_against_master(
    *,
    file_key: str,
    instance_node: dict[str, Any],
    master_node: dict[str, Any] | None,
    master_file_key: str | None,
    master_node_id: str | None,
    master_library_hash: str | None,
    master_published_key: str | None = None,
    master_component_key: str | None = None,
    master_component_set_key: str | None = None,
    current_ds_library_hashes: set[str] | None = None,
    current_ds_file_keys: set[str] | None = None,
    current_ds_published_keys: set[str] | None = None,
    component_set_node: dict[str, Any] | None = None,
) -> InstanceDiff:
    """Pure helper for tests and callers that already have both node payloads."""
    node_id = str(instance_node.get("id") or "")
    master = MasterRef(
        file_key=master_file_key,
        node_id=normalize_node_id(master_node_id) if master_node_id else None,
        library_hash=master_library_hash,
        published_key=master_published_key,
        component_key=master_component_key,
        component_set_key=master_component_set_key,
        is_resolvable=master_node is not None,
    )
    master.is_current_ds = _is_current_ds(
        master,
        current_ds_library_hashes=current_ds_library_hashes or set(),
        current_ds_file_keys=current_ds_file_keys or set(),
        current_ds_published_keys=current_ds_published_keys or set(),
    )
    return _build_instance_diff(
        instance=InstanceRef(file_key=file_key, node_id=normalize_node_id(node_id)),
        master=master,
        instance_node=instance_node,
        master_node=master_node,
        component_set_node=component_set_node,
    )


def _build_instance_diff(
    *,
    instance: InstanceRef,
    master: MasterRef,
    instance_node: dict[str, Any],
    master_node: dict[str, Any] | None,
    component_set_node: dict[str, Any] | None,
) -> InstanceDiff:
    properties = _property_diffs(master_node, instance_node)
    override_properties = [row.property for row in properties if row.is_override]
    return InstanceDiff(
        instance=instance,
        master=master,
        variant=_variant_info(instance_node, master_node, component_set_node),
        properties=properties,
        override_properties=override_properties,
    )


def _node_from_response(payload: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, Mapping):
        raise ValueError("Figma response did not include a nodes map")
    for key in (node_id, node_id.replace(":", "-")):
        entry = nodes.get(key)
        if isinstance(entry, Mapping):
            document = entry.get("document")
            if isinstance(document, dict):
                return document
    raise NodeNotFoundError(f"{node_id}: node not found in Figma response")


def _component_meta_for_instance(
    payload: Mapping[str, Any], instance_node: Mapping[str, Any]
) -> dict[str, Any]:
    component_id = instance_node.get("componentId")
    if not isinstance(component_id, str) or not component_id:
        return {}
    return _metadata_for_node(payload, "components", component_id)


def _component_set_meta_for_component(
    payload: Mapping[str, Any],
    component_meta: Mapping[str, Any],
) -> dict[str, Any]:
    component_set_id = _first_str(component_meta, "componentSetId", "component_set_id")
    if not component_set_id:
        return {}
    return _metadata_for_node(payload, "componentSets", component_set_id)


def _metadata_for_node(
    payload: Mapping[str, Any],
    map_name: str,
    node_id: str,
) -> dict[str, Any]:
    for values in _metadata_maps(payload, map_name):
        for key in _node_id_keys(node_id):
            value = values.get(key)
            if isinstance(value, dict):
                return value
    return {}


def _metadata_maps(payload: Mapping[str, Any], map_name: str) -> list[Mapping[str, Any]]:
    maps: list[Mapping[str, Any]] = []
    top_level = payload.get(map_name)
    if isinstance(top_level, Mapping):
        maps.append(top_level)
    nodes = payload.get("nodes")
    if isinstance(nodes, Mapping):
        for entry in nodes.values():
            if not isinstance(entry, Mapping):
                continue
            nested = entry.get(map_name)
            if isinstance(nested, Mapping):
                maps.append(nested)
    return maps


def _node_id_keys(node_id: str) -> tuple[str, str, str]:
    normalized = normalize_node_id(node_id)
    return (node_id, node_id.replace(":", "-"), normalized)


async def _fetch_optional_node_response(
    client: FigmaClient,
    file_key: str,
    node_id: str,
) -> dict[str, Any]:
    try:
        return await client.get_nodes_response(file_key, [node_id], depth=1)
    except httpx.HTTPStatusError:
        return {}


def _optional_node_from_response(
    payload: Mapping[str, Any],
    node_id: str,
) -> dict[str, Any] | None:
    if not payload:
        return None
    try:
        return _node_from_response(payload, normalize_node_id(node_id))
    except ValueError:
        return None


async def _resolve_master_ref(
    client: FigmaClient,
    *,
    file_key: str,
    instance_node: Mapping[str, Any],
    component_meta: dict[str, Any],
    component_set_meta: dict[str, Any],
    current_ds_library_hashes: set[str],
    current_ds_file_keys: set[str],
    current_ds_published_keys: set[str],
) -> MasterRef:
    component_id = _str_or_none(instance_node.get("componentId"))
    component_key = _first_str(component_meta, "key") or _str_or_none(
        instance_node.get("componentKey")
    )
    component_set_key = _first_str(component_set_meta, "key") or _first_str(
        component_meta,
        "componentSetKey",
        "component_set_key",
    )
    meta = dict(component_meta)
    if component_key and (not meta.get("file_key") or not meta.get("node_id")):
        remote_meta = await _fetch_component_meta(client, component_key)
        if remote_meta:
            meta = {**remote_meta, **meta}

    master_file_key = _first_str(meta, "file_key", "fileKey") or file_key
    master_node_id = _first_str(meta, "node_id", "nodeId") or component_id
    library_hash = _first_str(meta, "library_hash", "libraryHash")
    published_key = component_set_key or component_key
    master = MasterRef(
        file_key=master_file_key,
        node_id=normalize_node_id(master_node_id) if master_node_id else None,
        library_hash=library_hash,
        published_key=published_key,
        component_key=component_key,
        component_set_key=component_set_key,
        is_resolvable=False,
    )
    _update_master_ref_from_metadata(
        master,
        component_meta=component_meta,
        component_set_meta=component_set_meta,
        current_ds_library_hashes=current_ds_library_hashes,
        current_ds_file_keys=current_ds_file_keys,
        current_ds_published_keys=current_ds_published_keys,
    )
    return master


async def _resolve_master_ref_cached(
    client: FigmaClient,
    *,
    file_key: str,
    instance_node: Mapping[str, Any],
    component_meta: dict[str, Any],
    component_set_meta: dict[str, Any],
    current_ds_library_hashes: set[str],
    current_ds_file_keys: set[str],
    current_ds_published_keys: set[str],
    component_cache: dict[str, dict[str, Any]],
) -> MasterRef:
    component_key = _first_str(component_meta, "key") or _str_or_none(
        instance_node.get("componentKey")
    )
    component_set_key = _first_str(component_set_meta, "key") or _first_str(
        component_meta,
        "componentSetKey",
        "component_set_key",
    )
    meta = dict(component_meta)
    if component_key and (not meta.get("file_key") or not meta.get("node_id")):
        remote_meta = await _fetch_component_meta_cached(client, component_key, component_cache)
        if remote_meta:
            meta = {**remote_meta, **meta}

    master_file_key = _first_str(meta, "file_key", "fileKey") or file_key
    master_node_id = _first_str(meta, "node_id", "nodeId") or _str_or_none(
        instance_node.get("componentId")
    )
    master = MasterRef(
        file_key=master_file_key,
        node_id=normalize_node_id(master_node_id) if master_node_id else None,
        library_hash=_first_str(meta, "library_hash", "libraryHash"),
        published_key=component_set_key or component_key,
        component_key=component_key,
        component_set_key=component_set_key,
        is_resolvable=False,
    )
    _update_master_ref_from_metadata(
        master,
        component_meta=meta,
        component_set_meta=component_set_meta,
        current_ds_library_hashes=current_ds_library_hashes,
        current_ds_file_keys=current_ds_file_keys,
        current_ds_published_keys=current_ds_published_keys,
    )
    return master


def _update_master_ref_from_metadata(
    master: MasterRef,
    *,
    component_meta: Mapping[str, Any],
    component_set_meta: Mapping[str, Any],
    current_ds_library_hashes: set[str],
    current_ds_file_keys: set[str],
    current_ds_published_keys: set[str],
) -> None:
    component_key = _first_str(component_meta, "key")
    component_set_key = _first_str(component_set_meta, "key") or _first_str(
        component_meta,
        "componentSetKey",
        "component_set_key",
    )
    library_hash = _first_str(component_meta, "library_hash", "libraryHash")
    if component_key:
        master.component_key = component_key
    if component_set_key:
        master.component_set_key = component_set_key
    if library_hash:
        master.library_hash = library_hash
    master.published_key = master.component_set_key or master.component_key or master.published_key
    master.is_current_ds = _is_current_ds(
        master,
        current_ds_library_hashes=current_ds_library_hashes,
        current_ds_file_keys=current_ds_file_keys,
        current_ds_published_keys=current_ds_published_keys,
    )


def _is_current_ds(
    master: MasterRef,
    *,
    current_ds_library_hashes: set[str],
    current_ds_file_keys: set[str],
    current_ds_published_keys: set[str],
) -> bool:
    current_ds_identifiers = current_ds_library_hashes | current_ds_published_keys
    return any(
        (
            bool(master.library_hash and master.library_hash in current_ds_identifiers),
            bool(master.file_key and master.file_key in current_ds_file_keys),
            bool(master.published_key and master.published_key in current_ds_identifiers),
            bool(master.component_key and master.component_key in current_ds_identifiers),
            bool(master.component_set_key and master.component_set_key in current_ds_identifiers),
        )
    )


async def _fetch_optional_meta(fetch: Any, key: str | None) -> dict[str, Any]:
    if not key:
        return {}
    try:
        result = await fetch(key)
    except httpx.HTTPStatusError:
        return {}
    return result if isinstance(result, dict) else {}


async def _fetch_component_meta(client: FigmaClient, component_key: str) -> dict[str, Any]:
    return await _fetch_optional_meta(client.get_component, component_key)


async def _fetch_component_meta_cached(
    client: FigmaClient,
    component_key: str,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if component_key not in cache:
        cache[component_key] = await _fetch_component_meta(client, component_key)
    return cache[component_key]


async def _fetch_component_set_meta(
    client: FigmaClient,
    component_set_key: str | None,
) -> dict[str, Any]:
    return await _fetch_optional_meta(client.get_component_set, component_set_key)


async def _fetch_component_set_meta_cached(
    client: FigmaClient,
    component_set_key: str,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if component_set_key not in cache:
        cache[component_set_key] = await _fetch_component_set_meta(client, component_set_key)
    return cache[component_set_key]


async def _fetch_optional_nodes_response(
    client: FigmaClient,
    file_key: str,
    node_ids: list[str],
) -> dict[str, Any]:
    try:
        return await _fetch_nodes_response_chunks(client, file_key, node_ids)
    except httpx.HTTPStatusError:
        return {}


async def _fetch_nodes_response_chunks(
    client: FigmaClient,
    file_key: str,
    node_ids: list[str],
    *,
    chunk_size: int = NODE_FETCH_BATCH_SIZE,
) -> dict[str, Any]:
    merged: dict[str, Any] = {"nodes": {}}
    for chunk in _chunks(node_ids, chunk_size):
        payload = await client.get_nodes_response(file_key, chunk, depth=1)
        _merge_nodes_response(merged, payload)
    return merged


def _merge_nodes_response(target: dict[str, Any], payload: Mapping[str, Any]) -> None:
    for map_name in ("nodes", "components", "componentSets", "styles"):
        values = payload.get(map_name)
        if not isinstance(values, Mapping):
            continue
        target_map = target.setdefault(map_name, {})
        if isinstance(target_map, dict):
            target_map.update(values)


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


async def _fetch_optional_node(
    client: FigmaClient,
    file_key: str,
    node_id: str,
) -> dict[str, Any] | None:
    try:
        nodes = await client.get_nodes(file_key, [node_id], depth=1)
    except httpx.HTTPStatusError:
        return None
    node = nodes.get(normalize_node_id(node_id))
    return node if isinstance(node, dict) and node else None


def _property_diffs(
    master_node: dict[str, Any] | None,
    instance_node: dict[str, Any],
) -> list[PropertyDiff]:
    if master_node is None:
        return []
    master_values = _extract_property_values(master_node)
    instance_values = _extract_property_values(instance_node)
    properties = sorted(set(master_values) | set(instance_values))
    diffs = []
    for prop in properties:
        master_value = master_values.get(prop)
        instance_value = instance_values.get(prop)
        master_binding = _binding_for_property(master_node, prop)
        instance_binding = _binding_for_property(instance_node, prop)
        value_changed = master_value != instance_value
        binding_changed = _binding_identity(master_binding) != _binding_identity(instance_binding)
        if value_changed and binding_changed:
            kind: OverrideKind = "both"
        elif value_changed:
            kind = "value"
        elif binding_changed:
            kind = "binding"
        else:
            kind = "none"
        diffs.append(
            PropertyDiff(
                property=prop,
                master_value=master_value,
                master_binding=master_binding,
                instance_value=instance_value,
                instance_binding=instance_binding,
                is_override=kind != "none",
                override_kind=kind,
            )
        )
    return diffs


def _extract_property_values(node: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for prop in SCALAR_PROPERTIES:
        if prop in node or prop in (node.get("boundVariables") or {}):
            values[prop] = node.get(prop)
    radii = node.get("rectangleCornerRadii")
    if isinstance(radii, list):
        for prop, index in zip(CORNER_RADIUS_PROPERTIES, range(4), strict=True):
            if index < len(radii):
                values[prop] = radii[index]
    for prop in PAINT_PROPERTIES:
        if prop in node or prop in (node.get("boundVariables") or {}):
            values[prop] = _paint_values(node.get(prop))
    style = node.get("style")
    style_map = style if isinstance(style, Mapping) else {}
    for prop in TEXT_STYLE_PROPERTIES:
        if prop in node:
            values[prop] = node.get(prop)
        elif prop in style_map:
            values[prop] = style_map.get(prop)
        elif prop in (node.get("boundVariables") or {}):
            values[prop] = None
    for prop in LIST_PROPERTIES:
        if prop in node or prop in (node.get("boundVariables") or {}):
            values[prop] = _json_clean(node.get(prop))
    return values


def _paint_values(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    return [_strip_bound_variables(paint) for paint in value]


def _strip_bound_variables(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_bound_variables(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _strip_bound_variables(child)
            for key, child in value.items()
            if key != "boundVariables"
        }
    return value


def _json_clean(value: Any) -> Any:
    if isinstance(value, list):
        return [_json_clean(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_clean(child) for key, child in value.items()}
    return value


def _binding_for_property(node: Mapping[str, Any], prop: str) -> dict[str, Any] | None:
    bindings: list[dict[str, Any]] = []
    if prop in PAINT_PROPERTIES:
        paint_prop = "fill" if prop == "fills" else "stroke"
        paints = node.get(prop)
        if isinstance(paints, list):
            for index, paint in enumerate(paints):
                if not isinstance(paint, dict):
                    continue
                var_id = paint_bound_variable_id(dict(node), paint_prop, index, paint)
                binding = _binding_from_id(var_id, source=f"{prop}[{index}]")
                if binding:
                    bindings.append(binding)
        return _coalesce_bindings(bindings)

    bv = node.get("boundVariables")
    if not isinstance(bv, Mapping):
        return None
    entry = _bound_variable_entry(bv, prop)
    if entry is None and prop in CORNER_RADIUS_PROPERTIES:
        entry = _corner_radius_binding_entry(bv, prop)
    if entry is None:
        return None
    if isinstance(entry, list):
        for index, item in enumerate(entry):
            binding = _binding_from_entry(item, source=f"{prop}[{index}]")
            if binding:
                bindings.append(binding)
        return _coalesce_bindings(bindings)
    return _binding_from_entry(entry, source=prop)


def _bound_variable_entry(bound_variables: Mapping[str, Any], prop: str) -> Any:
    if prop in bound_variables:
        return bound_variables[prop]
    if prop == "fills":
        return bound_variables.get("fills")
    if prop == "strokes":
        return bound_variables.get("strokes")
    return None


def _corner_radius_binding_entry(bound_variables: Mapping[str, Any], prop: str) -> Any:
    radii = bound_variables.get("rectangleCornerRadii")
    if isinstance(radii, list):
        index = CORNER_RADIUS_PROPERTIES.index(prop)
        if index < len(radii):
            return radii[index]
    return None


def _binding_from_entry(entry: Any, *, source: str) -> dict[str, Any] | None:
    var_id = bound_variable_id(entry)
    if not var_id:
        return None
    binding = _binding_from_id(var_id, source=source)
    if binding is None:
        return None
    if isinstance(entry, Mapping):
        name = _first_str(entry, "name", "variable_name", "variableName")
        if name:
            binding["variable_name"] = name
    return binding


def _binding_from_id(var_id: str, *, source: str) -> dict[str, Any] | None:
    if not var_id:
        return None
    binding = {"variable_id": var_id, "source": source}
    library_hash = _library_hash(var_id)
    if library_hash:
        binding["library_hash"] = library_hash
    return binding


def _coalesce_bindings(bindings: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not bindings:
        return None
    if len(bindings) == 1:
        binding = dict(bindings[0])
        binding.pop("source", None)
        return binding
    return {"bindings": bindings}


def _binding_identity(binding: dict[str, Any] | None) -> Any:
    if binding is None:
        return None
    children = binding.get("bindings")
    if isinstance(children, list):
        return tuple(_binding_identity(child) for child in children if isinstance(child, dict))
    return (
        binding.get("source"),
        binding.get("variable_id"),
        binding.get("library_hash"),
    )


def _library_hash(var_id: str) -> str | None:
    return variable_library_hash(var_id)


def _variant_info(
    instance_node: Mapping[str, Any],
    master_node: Mapping[str, Any] | None,
    component_set_node: Mapping[str, Any] | None,
) -> VariantInfo:
    return VariantInfo(
        selected=_selected_component_properties(instance_node),
        available=_available_component_properties(master_node, component_set_node),
    )


def _selected_component_properties(instance_node: Mapping[str, Any]) -> dict[str, Any]:
    raw = instance_node.get("componentProperties")
    if not isinstance(raw, Mapping):
        return {}
    selected: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, Mapping) and "value" in value:
            selected[str(key)] = value.get("value")
        else:
            selected[str(key)] = _json_clean(value)
    return selected


def _available_component_properties(
    master_node: Mapping[str, Any] | None,
    component_set_node: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    definitions = None
    for node in (component_set_node, master_node):
        if node is None:
            continue
        raw = node.get("componentPropertyDefinitions")
        if isinstance(raw, Mapping):
            definitions = raw
            break
    if not isinstance(definitions, Mapping):
        return []
    available = []
    for name, definition in definitions.items():
        row = {"property": str(name)}
        if isinstance(definition, Mapping):
            row.update(_json_clean(dict(definition)))
        else:
            row["value"] = _json_clean(definition)
        available.append(row)
    return available


def _component_set_id(
    component_meta: Mapping[str, Any],
    master_node: Mapping[str, Any] | None,
) -> str | None:
    from_meta = _first_str(component_meta, "componentSetId", "component_set_id")
    if from_meta:
        return normalize_node_id(from_meta)
    if master_node is None:
        return None
    return _str_or_none(master_node.get("componentSetId"))


def _first_str(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        result = _str_or_none(value)
        if result:
            return result
    return None


def _str_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
