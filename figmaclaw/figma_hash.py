"""Hash computation for Figma page and frame nodes.

Two levels of hashing:

compute_page_hash() — structural identity (node_id, name, type, parent_id)
for visible FRAME and SECTION nodes. Detects frames added/removed/renamed/
reordered AND frames whose visibility flipped.

compute_frame_hash() — content hash per frame (depth-1 visible children:
names, types, text content, component references). Detects meaningful
changes within a frame while ignoring visual noise (position, size, style).
Used for surgical enrichment: only re-screenshot and re-describe frames
whose content actually changed.

compute_frame_hashes() — batch computation for all visible frames in a page.

Visibility semantics
--------------------
All three functions filter invisible nodes via :func:`figma_schema.is_visible`.
Two consequences follow:

* **Hiding a frame changes the page hash**, triggering a re-render that
  correctly drops the frame from the rendered markdown.
* **Renaming or mutating an invisible frame does NOT change the page hash**,
  avoiding wasted re-enrichment cycles on changes that have no visible
  effect in the rendered output.

Inherited visibility: if a parent SECTION is hidden, its children are
treated as hidden too even if they carry ``visible: true`` on themselves.
This matches Figma's canvas rendering semantics — hiding a group hides
everything underneath it.

Name stability
--------------
Hashes use **raw** names from the Figma API (``node.get("name", "")``), NOT
the normalized form from :func:`figma_schema.normalize_name`. This is
deliberate: applying normalization here would change every stored
``enriched_hash`` value in downstream repos and trigger mass re-enrichment.
Hash stability across code changes is the invariant; human readability is
the renderer's job, not the hash layer's.
"""

from __future__ import annotations

import hashlib
import json

from figmaclaw.figma_schema import STRUCTURAL_NODE_TYPES, is_visible

# Alias kept for any external importer of the old private name.
_STRUCTURAL_TYPES = STRUCTURAL_NODE_TYPES


def compute_page_hash(page_node: dict) -> str:
    """Compute a stable structural hash for a Figma CANVAS page node.

    Returns a 16-character lowercase hex string. The hash is stable
    regardless of child ordering in the source JSON.

    Includes visible FRAME and SECTION nodes at depth 1 and visible
    FRAME / SECTION grandchildren inside visible SECTIONs. Excludes:

    * Invisible nodes (``visible: false`` — see :func:`figma_schema.is_visible`).
    * Children of an invisible parent (inherited visibility).
    * Non-structural types (CONNECTOR, TEXT, VECTOR, COMPONENT*, …).
    """
    tuples: list[tuple[str, str, str, str]] = []
    page_id: str = page_node.get("id", "")

    for child in page_node.get("children", []):
        child_type = child.get("type", "")
        if child_type not in STRUCTURAL_NODE_TYPES:
            continue
        if not is_visible(child):
            # Hidden parent → skip this node AND its descendants.
            continue
        tuples.append((child["id"], child.get("name", ""), child_type, page_id))

        # Only descend into SECTIONs. Sub-frames inside a FRAME are
        # content (handled by compute_frame_hash), not page-level structure.
        if child_type != "SECTION":
            continue
        for grandchild in child.get("children", []):
            gc_type = grandchild.get("type", "")
            if gc_type not in STRUCTURAL_NODE_TYPES:
                continue
            if not is_visible(grandchild):
                continue
            tuples.append(
                (grandchild["id"], grandchild.get("name", ""), gc_type, child["id"])
            )

    # Sort for stability — order in the Figma JSON should not matter.
    canonical = json.dumps(sorted(tuples), separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest[:16]


def compute_frame_hash(frame_node: dict) -> str:
    """Compute a content hash for a single frame based on its depth-1 children.

    Hashes: visible child names, types, TEXT ``characters``, INSTANCE
    ``componentId``. Ignores: position, size, fills, strokes, effects,
    opacity, and **invisible children**.

    Hiding a text layer or component instance inside a frame changes the
    visual screenshot — the stored description becomes stale, so this
    hash must change to trigger re-description. Conversely, renaming an
    already-invisible child has no visible effect and must NOT change
    the hash.

    Returns an 8-character lowercase hex string.
    """
    parts: list[str] = [frame_node.get("name", "")]
    for child in frame_node.get("children", []):
        if not is_visible(child):
            continue
        child_type = child.get("type", "")
        parts.append(f"{child.get('name', '')}:{child_type}")
        if child_type == "TEXT":
            parts.append(child.get("characters", ""))
        if child_type == "INSTANCE":
            parts.append(child.get("componentId", ""))
    canonical = "|".join(sorted(parts))
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def compute_frame_hashes(page_node: dict) -> dict[str, str]:
    """Compute content hashes for all **visible** FRAME nodes in a page.

    Traverses SECTION → FRAME and top-level FRAME nodes, skipping any
    node whose ``visible`` flag is explicitly ``false`` and any node
    whose parent SECTION is hidden (inherited visibility).

    Returns ``{node_id: 8-char hex hash}``.
    """
    result: dict[str, str] = {}
    for child in page_node.get("children", []):
        child_type = child.get("type", "")
        if not is_visible(child):
            # Hidden parent → skip this child AND its descendants.
            continue
        if child_type == "FRAME":
            result[child["id"]] = compute_frame_hash(child)
        elif child_type == "SECTION":
            for grandchild in child.get("children", []):
                if grandchild.get("type") not in STRUCTURAL_NODE_TYPES:
                    continue
                if not is_visible(grandchild):
                    continue
                result[grandchild["id"]] = compute_frame_hash(grandchild)
    return result
