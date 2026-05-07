"""Shared primitives for audit-page migration setup commands."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from figmaclaw.audit import walk_nodes_with_context

JSONL_NODE_FIELDS = (
    "fills",
    "strokes",
    "boundVariables",
    "size",
    "absoluteBoundingBox",
    "paddingLeft",
    "paddingRight",
    "paddingTop",
    "paddingBottom",
    "itemSpacing",
    "cornerRadius",
    "rectangleCornerRadii",
    "characters",
    "style",
    "layoutMode",
    "primaryAxisAlignItems",
    "counterAxisAlignItems",
    "strokeWeight",
    "opacity",
    "componentId",
    "componentKey",
    "componentProperties",
    "overrides",
    "constraints",
    "layoutSizingHorizontal",
    "layoutSizingVertical",
    "primaryAxisSizingMode",
    "counterAxisSizingMode",
)

ALLOWED_CLONE_REST_TYPES = frozenset({"CANVAS", "FRAME", "SECTION"})


def annotate_component_keys(node: dict[str, Any], components: dict[str, Any]) -> None:
    """Attach publishable component keys to instance nodes when Figma returns metadata."""
    component_id = node.get("componentId")
    if isinstance(component_id, str) and component_id and "componentKey" not in node:
        component_meta = (
            components.get(component_id)
            or components.get(component_id.replace("-", ":"))
            or components.get(component_id.replace(":", "-"))
        )
        if isinstance(component_meta, dict) and component_meta.get("key"):
            node["componentKey"] = component_meta["key"]
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            annotate_component_keys(child, components)


def iter_node_records(
    node: dict[str, Any],
    *,
    root_node_id: str | None = None,
    ancestor_path: list[str] | None = None,
) -> Iterable[dict[str, Any]]:
    """Yield the node subtree in DFS order using the migration JSONL shape."""
    root_id = root_node_id or node.get("id")
    prefix = list(ancestor_path or [])
    for current, ancestors, _inside_instance in walk_nodes_with_context(node):
        record: dict[str, Any] = {
            "node_id": current.get("id"),
            "name": current.get("name"),
            "type": current.get("type"),
            "ancestor_path": prefix + [str(ancestor.get("name", "")) for ancestor in ancestors],
            "frame_node_id": root_id,
        }
        for key in JSONL_NODE_FIELDS:
            if key in current:
                record[key] = current[key]
        yield record


def record_to_jsonl_line(record: dict[str, Any]) -> str:
    """Serialize one record as JSONL, escaping line-separator codepoints for splitline readers."""
    return json.dumps(record, ensure_ascii=True) + "\n"


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.rstrip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            records.append(value)
    return records


def build_idmap_report(
    src_records: list[dict[str, Any]],
    dst_records: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build a source-to-destination id map and report structural divergences."""
    limit = min(len(src_records), len(dst_records))
    idmap: dict[str, str] = {}
    divergences: list[dict[str, Any]] = []

    for index in range(limit):
        src = src_records[index]
        dst = dst_records[index]
        src_id = src.get("node_id")
        dst_id = dst.get("node_id")
        src_name = src.get("name")
        dst_name = dst.get("name")
        src_type = src.get("type")
        dst_type = dst.get("type")
        if src_type != dst_type or src_name != dst_name:
            divergences.append(
                {
                    "index": index,
                    "src_id": src_id,
                    "dst_id": dst_id,
                    "src_type": src_type,
                    "dst_type": dst_type,
                    "src_name": src_name,
                    "dst_name": dst_name,
                }
            )
        if src_id and dst_id:
            idmap[str(src_id)] = str(dst_id)

    if len(src_records) != len(dst_records):
        divergences.append(
            {
                "index": -1,
                "kind": "length_mismatch",
                "src_count": len(src_records),
                "dst_count": len(dst_records),
            }
        )

    report = {
        "ok": not divergences,
        "src_records": len(src_records),
        "dst_records": len(dst_records),
        "idmap_entries": len(idmap),
        "divergence_count": len(divergences),
        "divergences": divergences,
    }
    return idmap, report


def default_clone_title(source_name: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    return f"Audit - {source_name} - {stamp}"


def clone_request_receipt(
    *,
    file_key: str,
    source_node: dict[str, Any],
    title: str,
    namespace: str,
    generated_js: str | None,
    destination_page: dict[str, Any] | None,
) -> dict[str, Any]:
    source_child_count = len(source_node.get("children") or [])
    created_new_page = destination_page is None
    return {
        "file_key": file_key,
        "source_node_id": source_node.get("id"),
        "source_node_name": source_node.get("name"),
        "source_node_type": source_node.get("type"),
        "source_child_count": source_child_count,
        "target_page_name": title,
        "destination_page_id": destination_page.get("id") if destination_page else None,
        "destination_page_name": destination_page.get("name") if destination_page else None,
        "created_new_page": created_new_page,
        "source_page_id": source_node.get("id"),
        "source_page_name": source_node.get("name"),
        "source_top_level_children": source_child_count,
        "namespace": namespace,
        "generated_js": generated_js,
    }


CLONE_SCRIPT_TEMPLATE = r"""
// Generated by figmaclaw audit-page emit-clone-script.
// Run in the Figma Plugin API runtime with the file open in edit mode.
const SOURCE_NODE_ID = __SOURCE_NODE_ID_JSON__;
const DESTINATION_PAGE_ID = __DESTINATION_PAGE_ID_JSON__;
const TARGET_PAGE_NAME = __TARGET_PAGE_NAME_JSON__;
const SOURCE_FILE_KEY = __FILE_KEY_JSON__;
const NAMESPACE = __NAMESPACE_JSON__;
const CHUNK_SIZE = 85000;

function walkPairs(src, dst, pairs) {
  pairs.push([src.id, dst.id]);
  const srcChildren = "children" in src ? src.children : [];
  const dstChildren = "children" in dst ? dst.children : [];
  const len = Math.min(srcChildren.length, dstChildren.length);
  for (let i = 0; i < len; i++) {
    walkPairs(srcChildren[i], dstChildren[i], pairs);
  }
}

function chunkString(value, size) {
  const chunks = [];
  for (let i = 0; i < value.length; i += size) {
    chunks.push(value.slice(i, i + size));
  }
  return chunks;
}

function readSPDChunks(prefix, countKey) {
  const count = Number(targetPage.getSharedPluginData(NAMESPACE, countKey) || "0");
  let value = "";
  for (let i = 0; i < count; i++) {
    value += targetPage.getSharedPluginData(NAMESPACE, `${prefix}.${i}`) || "";
  }
  return value;
}

function writeSPDChunks(prefix, countKey, value, size) {
  const oldCount = Number(targetPage.getSharedPluginData(NAMESPACE, countKey) || "0");
  const chunks = chunkString(value, size);
  targetPage.setSharedPluginData(NAMESPACE, countKey, String(chunks.length));
  for (let i = 0; i < chunks.length; i++) {
    targetPage.setSharedPluginData(NAMESPACE, `${prefix}.${i}`, chunks[i]);
  }
  for (let i = chunks.length; i < oldCount; i++) {
    targetPage.setSharedPluginData(NAMESPACE, `${prefix}.${i}`, "");
  }
  return chunks.length;
}

function existingIdMap() {
  const raw = readSPDChunks("idMap", "idMapChunkCount");
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch (err) {
    failures.push({
      sourceId: SOURCE_NODE_ID,
      name: "existing idMap",
      type: "SHARED_PLUGIN_DATA",
      message: `Ignoring unreadable existing idMap: ${String(err && err.message ? err.message : err)}`,
    });
    return {};
  }
}

function cloneIntoPage(source, targetPage, pairs, failures) {
  try {
    const cloned = source.clone();
    targetPage.appendChild(cloned);
    if ("x" in source && "x" in cloned) cloned.x = source.x;
    if ("y" in source && "y" in cloned) cloned.y = source.y;
    walkPairs(source, cloned, pairs);
    return cloned;
  } catch (err) {
    failures.push({
      sourceId: source.id,
      name: source.name,
      type: source.type,
      message: String(err && err.message ? err.message : err),
    });
    return null;
  }
}

const sourceNode = await figma.getNodeByIdAsync(SOURCE_NODE_ID);
if (!sourceNode || !["PAGE", "FRAME", "SECTION"].includes(sourceNode.type)) {
  throw new Error(`Source node ${SOURCE_NODE_ID} was not found or is not a PAGE, FRAME, or SECTION`);
}

let targetPage;
let createdNewPage = false;
if (DESTINATION_PAGE_ID) {
  targetPage = await figma.getNodeByIdAsync(DESTINATION_PAGE_ID);
  if (!targetPage || targetPage.type !== "PAGE") {
    throw new Error(`Destination page ${DESTINATION_PAGE_ID} was not found or is not a PAGE`);
  }
  if (sourceNode.type === "PAGE" && sourceNode.id === targetPage.id) {
    throw new Error("Refusing to clone a page into itself");
  }
} else {
  const existing = figma.root.children.find((page) => page.name === TARGET_PAGE_NAME);
  if (existing) {
    throw new Error(`Target page already exists: ${TARGET_PAGE_NAME}`);
  }
  targetPage = figma.createPage();
  targetPage.name = TARGET_PAGE_NAME;
  createdNewPage = true;
}

const pairs = [];
const failures = [];
let clonedRoot = null;
const clonedTopLevelIds = [];

if (sourceNode.type === "PAGE") {
  pairs.push([sourceNode.id, targetPage.id]);
  for (const child of sourceNode.children) {
    const cloned = cloneIntoPage(child, targetPage, pairs, failures);
    if (cloned) clonedTopLevelIds.push(cloned.id);
  }
} else {
  clonedRoot = cloneIntoPage(sourceNode, targetPage, pairs, failures);
}

const sourceIdsRaw = targetPage.getSharedPluginData(NAMESPACE, "sourceNodeIds") || "[]";
let sourceNodeIds;
try {
  sourceNodeIds = JSON.parse(sourceIdsRaw);
  if (!Array.isArray(sourceNodeIds)) sourceNodeIds = [];
} catch (_err) {
  sourceNodeIds = [];
}
if (!sourceNodeIds.includes(sourceNode.id)) sourceNodeIds.push(sourceNode.id);
targetPage.setSharedPluginData(NAMESPACE, "sourceNodeIds", JSON.stringify(sourceNodeIds));

const newIdMap = Object.fromEntries(pairs);
const idMap = {...existingIdMap(), ...newIdMap};
const idMapJson = JSON.stringify(idMap);
const idMapChunks = writeSPDChunks("idMap", "idMapChunkCount", idMapJson, CHUNK_SIZE);

targetPage.setSharedPluginData(NAMESPACE, "sourceFileKey", SOURCE_FILE_KEY);
targetPage.setSharedPluginData(NAMESPACE, "sourceNodeId", sourceNode.id);
targetPage.setSharedPluginData(NAMESPACE, "sourceNodeType", sourceNode.type);
targetPage.setSharedPluginData(NAMESPACE, "createdAt", new Date().toISOString());
targetPage.setSharedPluginData(NAMESPACE, "idMapLength", String(idMapJson.length));

await figma.setCurrentPageAsync(targetPage);
const clonedRootId = sourceNode.type === "PAGE" ? targetPage.id : (clonedRoot ? clonedRoot.id : null);
return {
  ok: failures.length === 0,
  sourceNodeId: sourceNode.id,
  sourceNodeType: sourceNode.type,
  clonedRootId,
  clonedTopLevelIds,
  targetPageId: targetPage.id,
  targetPageName: targetPage.name,
  createdNewPage,
  topLevelChildren: targetPage.children.length,
  idMapEntries: Object.keys(idMap).length,
  idMapEntriesAdded: pairs.length,
  idMapBytes: idMapJson.length,
  idMapChunks,
  failures,
};
"""


def render_clone_script(
    *,
    file_key: str,
    source_node_id: str,
    title: str,
    namespace: str,
    destination_page_id: str | None,
) -> str:
    return (
        CLONE_SCRIPT_TEMPLATE.replace("__FILE_KEY_JSON__", json.dumps(file_key))
        .replace("__SOURCE_NODE_ID_JSON__", json.dumps(source_node_id))
        .replace("__DESTINATION_PAGE_ID_JSON__", json.dumps(destination_page_id))
        .replace("__TARGET_PAGE_NAME_JSON__", json.dumps(title))
        .replace("__NAMESPACE_JSON__", json.dumps(namespace))
        .lstrip()
    )
