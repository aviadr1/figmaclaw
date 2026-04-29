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
the renderer's job, not the hash layer's. Canon: NC-1 and HSH-1 define the
coverage contract between renderable units and hash inputs.
"""

from __future__ import annotations

import hashlib
import json

from figmaclaw.figma_schema import COMPONENT_NODE_TYPES, STRUCTURAL_NODE_TYPES, is_visible

# Alias kept for any external importer of the old private name.
_STRUCTURAL_TYPES = STRUCTURAL_NODE_TYPES


def compute_page_hash(page_node: dict) -> str:
    """Compute a stable structural hash for a Figma CANVAS page node.

    Returns a 16-character lowercase hex string. The hash is stable
    regardless of child ordering in the source JSON.

    Includes:

    * Visible FRAME and SECTION nodes at depth 1.
    * Visible FRAME / SECTION grandchildren inside visible SECTIONs.
    * Visible COMPONENT and COMPONENT_SET nodes at depth 1 (canon NC-1; otherwise the
      hash collapses to the empty-list digest for component-only pages
      where designers placed COMPONENT_SETs directly on the canvas — Tier 2
      of the refresh ladder then short-circuits forever and the page
      never gets a local .md, manifest md_path stays null, and the user
      hits the "Tooltip / Help icon / Logo missing" partial-pull bug).
    * Visible COMPONENT and COMPONENT_SET grandchildren inside visible
      SECTIONs (so the hash reflects component renames inside library
      sections too).
    * Visible COMPONENT children of any COMPONENT_SET (variants, canon HSH-1),
      regardless of where the COMPONENT_SET sits on the page. Without
      this, adding / removing / renaming a variant doesn't bump the
      hash and the rendered variant table on disk goes stale silently.

    Excludes:

    * Invisible nodes (``visible: false`` — see :func:`figma_schema.is_visible`).
    * Children of an invisible parent (inherited visibility).
    * Non-renderable types (CONNECTOR, TEXT, VECTOR, …).
    * Sub-frames inside a top-level FRAME — those are screen content,
      hashed separately by :func:`compute_frame_hash`.
    """
    tuples: list[tuple[str, str, str, str]] = []
    page_id: str = page_node.get("id", "")

    def _emit_variants(component_set: dict) -> None:
        """Append (id, name, type, parent_id) tuples for visible COMPONENT
        children of *component_set*. Designed to fire for COMPONENT_SETs at
        depth 1 and inside SECTIONs alike."""
        for variant in component_set.get("children", []):
            if variant.get("type") != "COMPONENT":
                continue
            if not is_visible(variant):
                continue
            tuples.append(
                (
                    variant["id"],
                    variant.get("name", ""),
                    "COMPONENT",
                    component_set["id"],
                )
            )

    for child in page_node.get("children", []):
        child_type = child.get("type", "")
        if child_type not in STRUCTURAL_NODE_TYPES and child_type not in COMPONENT_NODE_TYPES:
            continue
        if not is_visible(child):
            # Hidden parent → skip this node AND its descendants.
            continue
        tuples.append((child["id"], child.get("name", ""), child_type, page_id))

        if child_type == "COMPONENT_SET":
            # Top-level COMPONENT_SET: include its variants so adding /
            # removing / renaming one bumps the page hash.
            _emit_variants(child)
            continue

        # FRAMEs: sub-frames are screen content (compute_frame_hash).
        if child_type != "SECTION":
            continue

        for grandchild in child.get("children", []):
            gc_type = grandchild.get("type", "")
            if gc_type not in STRUCTURAL_NODE_TYPES and gc_type not in COMPONENT_NODE_TYPES:
                continue
            if not is_visible(grandchild):
                continue
            tuples.append((grandchild["id"], grandchild.get("name", ""), gc_type, child["id"]))
            if gc_type == "COMPONENT_SET":
                # SECTION-wrapped COMPONENT_SET (the common ✅ Avatar /
                # ✅ Button shape): same variant detection as top-level.
                _emit_variants(grandchild)

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
    """Compute content hashes for all **visible** rendered units in a page.

    "Rendered unit" includes the canon NC-1 renderable unit set:

    * Top-level FRAMEs and FRAMEs nested inside a visible SECTION (screen
      content; hashed via :func:`compute_frame_hash`).
    * Top-level COMPONENT and COMPONENT_SET nodes (component-library
      content placed directly on the canvas — see ✅ Tooltip & Help
      icon, ☼ Logo, ☼ App Icon).
    * COMPONENT and COMPONENT_SET nodes nested inside a visible SECTION
      (the common ✅ Avatar / ✅ Button shape).

    Hidden nodes and the descendants of hidden parents are skipped
    (inherited visibility).

    Returns ``{node_id: 8-char hex hash}``. The id set matches the union
    of frames rendered to screen .md files and components rendered to
    component .md files, so per-unit staleness is uniformly trackable.
    """
    result: dict[str, str] = {}
    for child in page_node.get("children", []):
        child_type = child.get("type", "")
        if not is_visible(child):
            # Hidden parent → skip this child AND its descendants.
            continue
        if child_type == "FRAME" or child_type in COMPONENT_NODE_TYPES:
            result[child["id"]] = compute_frame_hash(child)
        elif child_type == "SECTION":
            for grandchild in child.get("children", []):
                gc_type = grandchild.get("type", "")
                if gc_type not in STRUCTURAL_NODE_TYPES and gc_type not in COMPONENT_NODE_TYPES:
                    continue
                if not is_visible(grandchild):
                    continue
                result[grandchild["id"]] = compute_frame_hash(grandchild)
    return result
