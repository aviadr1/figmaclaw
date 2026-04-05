"""Hash computation for Figma page and frame nodes.

Two levels of hashing:

compute_page_hash() — structural identity (node_id, name, type, parent_id) for
FRAME and SECTION nodes. Detects frames added/removed/renamed/reordered.

compute_frame_hash() — content hash per frame (depth-1 children: names, types,
text content, component references). Detects meaningful changes within a frame
while ignoring visual noise (position, size, style). Used for surgical enrichment:
only re-screenshot and re-describe frames whose content actually changed.

compute_frame_hashes() — batch computation for all frames in a page.

IMPORTANT — hash stability: the hash bytes depend on the exact string
representation of raw Figma names (``node.get("name", "")``). We intentionally
do NOT pass these through :func:`figma_schema.normalize_name` here, because
that would change every stored ``enriched_hash`` value and trigger mass
re-enrichment. Use :func:`figma_schema.is_visible` for the visibility
predicate so all call sites agree on what "visible" means, but keep name
handling raw.
"""

from __future__ import annotations

import hashlib
import json

from figmaclaw.figma_schema import STRUCTURAL_NODE_TYPES, is_visible

# Alias kept for any external importer of the old name.
_STRUCTURAL_TYPES = STRUCTURAL_NODE_TYPES


def compute_page_hash(page_node: dict) -> str:
    """Compute a stable structural hash for a Figma CANVAS page node.

    Returns a 16-character lowercase hex string.
    The hash is stable regardless of child ordering in the source JSON.
    """
    tuples: list[tuple[str, str, str, str]] = []
    page_id: str = page_node.get("id", "")

    for child in page_node.get("children", []):
        child_type = child.get("type", "")
        if child_type not in _STRUCTURAL_TYPES:
            continue
        tuples.append((child["id"], child.get("name", ""), child_type, page_id))

        for grandchild in child.get("children", []):
            gc_type = grandchild.get("type", "")
            if gc_type in _STRUCTURAL_TYPES:
                tuples.append((grandchild["id"], grandchild.get("name", ""), gc_type, child["id"]))

    # Sort for stability — order in the Figma JSON should not matter
    canonical = json.dumps(sorted(tuples), separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest[:16]


def compute_frame_hash(frame_node: dict) -> str:
    """Compute a content hash for a single frame based on its depth-1 children.

    Hashes: child names, types, TEXT characters, INSTANCE componentId.
    Ignores: position, size, fills, strokes, effects, opacity.

    Returns an 8-character lowercase hex string.
    """
    parts: list[str] = [frame_node.get("name", "")]
    for child in frame_node.get("children", []):
        child_type = child.get("type", "")
        parts.append(f"{child.get('name', '')}:{child_type}")
        if child_type == "TEXT":
            parts.append(child.get("characters", ""))
        if child_type == "INSTANCE":
            parts.append(child.get("componentId", ""))
    canonical = "|".join(sorted(parts))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def compute_frame_hashes(page_node: dict) -> dict[str, str]:
    """Compute content hashes for all FRAME nodes in a page.

    Traverses SECTION → FRAME and top-level FRAME nodes.
    Returns {node_id: 8-char hex hash}.
    """
    result: dict[str, str] = {}
    for child in page_node.get("children", []):
        child_type = child.get("type", "")
        if child_type == "FRAME" and is_visible(child):
            result[child["id"]] = compute_frame_hash(child)
        elif child_type == "SECTION":
            for grandchild in child.get("children", []):
                if grandchild.get("type") in STRUCTURAL_NODE_TYPES and is_visible(grandchild):
                    result[grandchild["id"]] = compute_frame_hash(grandchild)
    return result
