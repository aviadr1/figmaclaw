"""Structural hash computation for Figma page nodes.

Hashes only structural identity (node_id, name, type, parent_id) for FRAME
and SECTION nodes. Visual-only properties (positions, fills, styles) are
intentionally excluded — only structural changes trigger markdown regeneration.
"""

from __future__ import annotations

import hashlib
import json


_STRUCTURAL_TYPES = frozenset({"FRAME", "SECTION"})


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
