"""Instance/master property diff primitives."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from figmaclaw.figma_client import FigmaClient, normalize_node_id
from figmaclaw.token_scan import bound_variable_id, paint_bound_variable_id

OverrideKind = Literal["none", "value", "binding", "both"]

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


async def diff_instance_against_master(
    client: FigmaClient,
    file_key: str,
    instance_node_id: str,
    *,
    current_ds_library_hashes: set[str],
) -> InstanceDiff:
    """Fetch an instance and compare supported properties against its master."""
    normalized_instance_id = normalize_node_id(instance_node_id)
    instance_payload = await client.get_nodes_response(file_key, [instance_node_id], depth=1)
    instance_node = _node_from_response(instance_payload, normalized_instance_id)
    if instance_node.get("type") != "INSTANCE":
        raise ValueError(f"{instance_node_id}: expected INSTANCE, got {instance_node.get('type')}")

    component_meta = _component_meta_for_instance(instance_payload, instance_node)
    master_ref = await _resolve_master_ref(
        client,
        file_key=file_key,
        instance_node=instance_node,
        component_meta=component_meta,
        current_ds_library_hashes=current_ds_library_hashes,
    )

    master_node: dict[str, Any] | None = None
    component_set_node: dict[str, Any] | None = None
    if master_ref.file_key and master_ref.node_id:
        master_node = await _fetch_optional_node(client, master_ref.file_key, master_ref.node_id)
        master_ref.is_resolvable = master_node is not None
        component_set_id = _component_set_id(component_meta, master_node)
        if component_set_id and master_ref.file_key:
            component_set_node = await _fetch_optional_node(
                client, master_ref.file_key, component_set_id
            )

    return InstanceDiff(
        instance=InstanceRef(file_key=file_key, node_id=normalized_instance_id),
        master=master_ref,
        variant=_variant_info(instance_node, master_node, component_set_node),
        properties=_property_diffs(master_node, instance_node),
    )


def diff_nodes_against_master(
    *,
    file_key: str,
    instance_node: dict[str, Any],
    master_node: dict[str, Any] | None,
    master_file_key: str | None,
    master_node_id: str | None,
    master_library_hash: str | None,
    master_published_key: str | None = None,
    current_ds_library_hashes: set[str] | None = None,
    component_set_node: dict[str, Any] | None = None,
) -> InstanceDiff:
    """Pure helper for tests and callers that already have both node payloads."""
    hashes = current_ds_library_hashes or set()
    node_id = str(instance_node.get("id") or "")
    return InstanceDiff(
        instance=InstanceRef(file_key=file_key, node_id=normalize_node_id(node_id)),
        master=MasterRef(
            file_key=master_file_key,
            node_id=normalize_node_id(master_node_id) if master_node_id else None,
            library_hash=master_library_hash,
            published_key=master_published_key,
            is_current_ds=bool(master_library_hash and master_library_hash in hashes),
            is_resolvable=master_node is not None,
        ),
        variant=_variant_info(instance_node, master_node, component_set_node),
        properties=_property_diffs(master_node, instance_node),
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
    raise ValueError(f"{node_id}: node not found in Figma response")


def _component_meta_for_instance(
    payload: Mapping[str, Any], instance_node: Mapping[str, Any]
) -> dict[str, Any]:
    component_id = instance_node.get("componentId")
    if not isinstance(component_id, str) or not component_id:
        return {}
    components = payload.get("components")
    if not isinstance(components, Mapping):
        return {}
    for key in (component_id, component_id.replace(":", "-"), normalize_node_id(component_id)):
        value = components.get(key)
        if isinstance(value, dict):
            return value
    return {}


async def _resolve_master_ref(
    client: FigmaClient,
    *,
    file_key: str,
    instance_node: Mapping[str, Any],
    component_meta: dict[str, Any],
    current_ds_library_hashes: set[str],
) -> MasterRef:
    component_id = _str_or_none(instance_node.get("componentId"))
    published_key = _first_str(
        component_meta,
        "componentSetKey",
        "component_set_key",
        "key",
    ) or _str_or_none(instance_node.get("componentKey"))
    meta = dict(component_meta)
    if published_key and (not meta.get("file_key") or not meta.get("node_id")):
        remote_meta = await _fetch_component_meta(client, published_key)
        if remote_meta:
            meta = {**remote_meta, **meta}

    master_file_key = _first_str(meta, "file_key", "fileKey") or file_key
    master_node_id = _first_str(meta, "node_id", "nodeId") or component_id
    library_hash = _first_str(meta, "library_hash", "libraryHash") or published_key
    return MasterRef(
        file_key=master_file_key,
        node_id=normalize_node_id(master_node_id) if master_node_id else None,
        library_hash=library_hash,
        published_key=published_key,
        is_current_ds=bool(library_hash and library_hash in current_ds_library_hashes),
        is_resolvable=False,
    )


async def _fetch_component_meta(client: FigmaClient, component_key: str) -> dict[str, Any]:
    try:
        return await client.get_component(component_key)
    except httpx.HTTPStatusError:
        return {}


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
    properties = sorted(
        set(_extract_property_values(master_node)) | set(_extract_property_values(instance_node))
    )
    diffs = []
    for prop in properties:
        master_value = _extract_property_value(master_node, prop)
        instance_value = _extract_property_value(instance_node, prop)
        master_binding = _binding_for_property(master_node, prop)
        instance_binding = _binding_for_property(instance_node, prop)
        value_changed = master_value != instance_value
        binding_changed = master_binding != instance_binding
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


def _extract_property_value(node: Mapping[str, Any], prop: str) -> Any:
    return _extract_property_values(node).get(prop)


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


def _library_hash(var_id: str) -> str | None:
    inner = var_id.removeprefix("VariableID:")
    if "/" not in inner:
        return None
    return inner.split("/", 1)[0]


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
