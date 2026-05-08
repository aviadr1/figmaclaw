"""Build and emit Figma component-instance swap batches for audit-page migrations.

The public CLI lives in :mod:`figmaclaw.commands.audit_page` (subcommand
``swap``); this module owns the pydantic schema and the JS template the
command emits. The swap step is the last forced ``use_figma`` touchpoint in
the migration pipeline before #162 — every row consumed here is mechanical:
look up source clone-id via idMap, import the new component_set, pick a
variant via the rule, ``createInstance``, copy preserve-listed props,
``parent.insertChild(oldIdx, new)``, ``old.remove()``.

Hard-rule contracts inherited from the linear-git migration practice and
restated here so they are local-readable for reviewers:

* **F17 — never ``.detach()``** anywhere in the emitted JS. Detaching a
  component instance loses every override and breaks the migration. The
  emitted script must use createInstance/insertChild/remove only.
* **F22 — overrides should be empty after swap.** A correct swap copies only
  design-intent props (text, show-X booleans, variant assignments). Any
  paint/binding override is a bug.
* **F30 — never ``throw`` on hardFailures > 0.** Per-row try/catch wraps each
  swap; the script returns aggregate stats. The caller (``--continue-on-error``
  + ``--execute``) decides whether the run as a whole fails.

The accepted schema below covers the row shape produced by the consumer
repo's ``scripts/build_swap_manifest.py`` plus a versioned wrapper:

::

    {
      "schema_version": 1,
      "kind": "figmaclaw.audit_page_swap.manifest",
      "file_key": "rvBhmhkDGFiZe6cDnG6SGU",
      "page_node_id": "9559:29",
      "namespace": "login_signup_onboarding_2026_05_08",
      "rows": [
        {
          "src": "8102:1990",
          "oldCid": "8009:29",
          "newKey": "e81fbd3e7c55508994f4630923b16d61f349eabf",
          "variants": {"Type": "Logo", "Colored": "True"},
          "props": {},
          "preserveText": true,
          "preserveSizing": true
        }
      ]
    }

A bare ``[{"src": ..., "newKey": ...}, ...]`` list is also accepted for the
unwrapped resolver-output case.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from figmaclaw.figma_js import READ_SPD_CHUNKS_JS, WRITE_SPD_CHUNKS_JS

AUDIT_PAGE_SWAP_SCHEMA_VERSION = 1


class SwapRow(BaseModel):
    """One per-instance swap intent.

    ``src`` is the SOURCE-FILE node id (the original instance on the live
    page). The emitted script resolves it to the audit-page clone via the
    idMap stored in shared plugin data. ``newKey`` is the publishable key of
    the TapIn component_set to import. ``variants`` is the resolved
    ``{axis: value}`` assignment to set on the new instance after creation.
    ``props`` is an optional ``{component-property-name: value}`` map for
    boolean / text overrides the new component_set publishes.

    ``preserveText`` keeps OLD ``characters`` overrides on text children
    whose names line up between OLD and NEW. ``preserveSizing`` keeps OLD
    FILL/HUG layout-sizing settings. Both default true since the dominant
    failure mode is destroying user-set text/sizing.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    src: str = Field(min_length=1, alias="src")
    new_key: str = Field(min_length=1, alias="newKey")
    old_component_id: str | None = Field(default=None, alias="oldCid")
    variants: dict[str, str] = Field(default_factory=dict)
    props: dict[str, Any] = Field(default_factory=dict)
    preserve_text: bool = Field(default=True, alias="preserveText")
    preserve_sizing: bool = Field(default=True, alias="preserveSizing")
    notes: str | None = None

    @field_validator("variants")
    @classmethod
    def _variants_must_be_str_str(cls, value: dict[str, str]) -> dict[str, str]:
        for k, v in value.items():
            if not isinstance(k, str) or not k:
                raise ValueError("variants axis names must be non-empty strings")
            if not isinstance(v, str):
                raise ValueError("variants axis values must be strings")
        return value


class SwapManifest(BaseModel):
    """Versioned swap manifest the audit-page swap CLI consumes."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = AUDIT_PAGE_SWAP_SCHEMA_VERSION
    kind: Literal["figmaclaw.audit_page_swap.manifest"] = "figmaclaw.audit_page_swap.manifest"
    file_key: str | None = None
    page_node_id: str | None = None
    namespace: str | None = None
    rows: list[SwapRow] = Field(default_factory=list)


def load_swap_manifest(payload: Any) -> SwapManifest:
    """Load a versioned manifest or a bare list of rows.

    Refuses on duplicate ``src`` values: a swap that ran once removed the
    OLD instance, so a re-run with the same ``src`` would silently report
    ``skipped_no_clone`` and the operator would see ``applied: 0`` without a
    clear cause. Catching duplicates at load time makes the failure
    actionable. (Issue #167 review finding #4.)
    """
    if isinstance(payload, dict) and "rows" in payload:
        manifest = SwapManifest.model_validate(payload)
    elif isinstance(payload, list):
        manifest = SwapManifest(rows=[SwapRow.model_validate(row) for row in payload])
    else:
        raise ValueError(
            "expected a versioned audit-page swap manifest or a JSON list of swap rows"
        )
    _reject_duplicate_src(manifest.rows)
    return manifest


def _reject_duplicate_src(rows: list[SwapRow]) -> None:
    seen: dict[str, int] = {}
    duplicates: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.src in seen:
            duplicates.setdefault(row.src, [seen[row.src]]).append(index)
        else:
            seen[row.src] = index
    if duplicates:
        offenders = ", ".join(f"{src}@rows{idxs}" for src, idxs in sorted(duplicates.items()))
        raise ValueError(
            f"swap manifest contains duplicate src values: {offenders}; "
            "the second occurrence would silently skip after the first row "
            "removed the OLD instance"
        )


def _row_to_writer(row: SwapRow) -> dict[str, Any]:
    return {
        "src": row.src,
        "newKey": row.new_key,
        "oldCid": row.old_component_id,
        "variants": row.variants,
        "props": row.props,
        "preserveText": row.preserve_text,
        "preserveSizing": row.preserve_sizing,
    }


# JS template — F17/F22/F30 -compliant ----------------------------------------
#
# The template is intentionally inline-readable. Reviewers should be able to
# scan it once and confirm:
#   - no .detach() anywhere
#   - per-row try/catch that increments stats counters
#   - no terminal `throw` based on aggregate hardFailures
#   - every successful row updates the SPD idMap so apply-tokens runs
#     against the swapped instance ids


AUDIT_PAGE_SWAP_JS_TEMPLATE = r"""
// Generated by figmaclaw audit-page swap.
// Run in the Figma Plugin API runtime with the file open in edit mode.
// Hard-rule contracts:
//   F17 — never .detach() anywhere
//   F22 — overrides on the new instance must be empty except design intent
//   F30 — never throw on partial failure; return aggregate stats instead
const TARGET_PAGE_ID = __TARGET_PAGE_ID__;
const NAMESPACE = __NAMESPACE__;
const ROWS = __ROWS__;

const targetPage = await figma.getNodeByIdAsync(TARGET_PAGE_ID);
if (!targetPage) throw new Error(`target page not found: ${TARGET_PAGE_ID}`);
if (typeof targetPage.loadAsync === "function") await targetPage.loadAsync();

__READ_SPD_CHUNKS_JS__

__WRITE_SPD_CHUNKS_JS__

const rawIdMap = readSPDChunks("idMap", "idMapChunkCount");
if (!rawIdMap) {
  throw new Error(`missing idMap SharedPluginData in namespace ${NAMESPACE}`);
}
const idMap = JSON.parse(rawIdMap);
// An empty idMap is an init-failure condition (audit page wasn't cloned
// against this namespace, or operator pointed at the wrong page). Hard-fail
// here rather than silently skipping every row downstream — see #167 review.
if (!idMap || typeof idMap !== "object" || Object.keys(idMap).length === 0) {
  throw new Error(`empty idMap in namespace ${NAMESPACE}; clone the source page first or check the audit page id`);
}

// Cache imported component_sets per newKey to avoid duplicate imports.
const componentSetCache = {};
async function importNewSet(newKey) {
  if (componentSetCache[newKey] !== undefined) return componentSetCache[newKey];
  try {
    const cs = await figma.importComponentSetByKeyAsync(newKey);
    componentSetCache[newKey] = cs;
    return cs;
  } catch (err) {
    componentSetCache[newKey] = null;
    return null;
  }
}

// Pick a variant child of *componentSet* whose published variant axes match
// *variants*. Returns null when no exact match is found.
function pickVariantChild(componentSet, variants) {
  if (!componentSet || !componentSet.children) return null;
  const wanted = Object.entries(variants || {});
  if (wanted.length === 0) return componentSet.defaultVariant || componentSet.children[0] || null;
  for (const child of componentSet.children) {
    // child.name like "Type=Logo, Colored=True"
    const pairs = (child.name || "").split(",").map((s) => s.trim());
    const have = {};
    for (const pair of pairs) {
      const idx = pair.indexOf("=");
      if (idx < 0) continue;
      have[pair.slice(0, idx).trim()] = pair.slice(idx + 1).trim();
    }
    let hit = true;
    for (const [axis, value] of wanted) {
      if (have[axis] !== value) { hit = false; break; }
    }
    if (hit) return child;
  }
  return null;
}

const stats = {
  applied: 0,
  skipped_no_clone: 0,
  skipped_no_set: 0,
  skipped_no_variant: 0,
  skipped_no_parent: 0,
  errors: 0,
};
const errorsSample = [];
const skipsSample = [];
function recordSkip(reason, row) {
  if (skipsSample.length < 20) {
    skipsSample.push({
      reason,
      src: row && row.src,
      newKey: row && row.newKey,
      oldCid: row && row.oldCid,
    });
  }
}
const newIdMapAdditions = {};

for (const row of ROWS) {
  try {
    const cloneId = idMap[row.src];
    if (!cloneId) { stats.skipped_no_clone++; recordSkip("no_clone", row); continue; }
    const oldInstance = await figma.getNodeByIdAsync(cloneId);
    if (!oldInstance) { stats.skipped_no_clone++; recordSkip("no_clone", row); continue; }
    const componentSet = await importNewSet(row.newKey);
    if (!componentSet) { stats.skipped_no_set++; recordSkip("no_set", row); continue; }

    const variantChild = pickVariantChild(componentSet, row.variants || {});
    if (!variantChild) { stats.skipped_no_variant++; recordSkip("no_variant", row); continue; }

    const newInstance = variantChild.createInstance();
    const parent = oldInstance.parent;
    const oldIdx = parent && parent.children ? parent.children.indexOf(oldInstance) : -1;
    if (!parent || oldIdx < 0) { stats.skipped_no_parent++; recordSkip("no_parent", row); continue; }

    // Place the new instance at the old position.
    if (typeof parent.insertChild === "function") {
      parent.insertChild(oldIdx, newInstance);
    } else {
      parent.appendChild(newInstance);
    }

    // Mirror geometry — same x/y/size/relativeTransform when possible. We
    // never call setRelativeTransform with a stale matrix; fall back to
    // setting x/y so auto-layout parents reflow correctly.
    if ("x" in oldInstance && "x" in newInstance) newInstance.x = oldInstance.x;
    if ("y" in oldInstance && "y" in newInstance) newInstance.y = oldInstance.y;

    if (row.preserveSizing) {
      const sizingProps = [
        "layoutSizingHorizontal",
        "layoutSizingVertical",
        "primaryAxisSizingMode",
        "counterAxisSizingMode",
      ];
      for (const sp of sizingProps) {
        if (sp in oldInstance && sp in newInstance) {
          try { newInstance[sp] = oldInstance[sp]; } catch (_e) { /* ignore */ }
        }
      }
    }

    // Apply rule-driven component property overrides.
    if (row.props && Object.keys(row.props).length > 0) {
      try { newInstance.setProperties(row.props); } catch (_e) { /* non-fatal */ }
    }

    // Preserve text content on matching-name text descendants.
    if (row.preserveText && "findAllWithCriteria" in oldInstance) {
      try {
        const oldTexts = oldInstance.findAllWithCriteria({ types: ["TEXT"] }) || [];
        const newTexts = newInstance.findAllWithCriteria({ types: ["TEXT"] }) || [];
        const newByName = {};
        for (const t of newTexts) newByName[t.name || ""] = newByName[t.name || ""] || t;
        for (const o of oldTexts) {
          const target = newByName[o.name || ""];
          if (!target) continue;
          try {
            // loadFontAsync may reject for unloadable fonts — F30 says we
            // never let that fail the whole batch, just this row's text
            // preservation.
            const style = target.fontName || o.fontName;
            if (style && style !== figma.mixed) await figma.loadFontAsync(style);
            target.characters = o.characters || "";
          } catch (_e) { /* ignore per-text font failures */ }
        }
      } catch (_e) { /* non-fatal */ }
    }

    newIdMapAdditions[row.src] = newInstance.id;
    oldInstance.remove();
    stats.applied++;
  } catch (err) {
    stats.errors++;
    if (errorsSample.length < 20) {
      errorsSample.push({
        src: row && row.src,
        newKey: row && row.newKey,
        error: String(err && err.message ? err.message : err),
      });
    }
  }
}

// Persist the updated idMap so apply-tokens runs target the NEW instances.
if (Object.keys(newIdMapAdditions).length > 0) {
  const merged = { ...idMap, ...newIdMapAdditions };
  const payload = JSON.stringify(merged);
  writeSPDChunks("idMap", "idMapChunkCount", payload, 85000);
}

// `ok` mirrors apply-tokens' hardFailures discipline: a batch that skipped
// every row (e.g. wrong audit page, missing component_set, no variant match)
// is NOT a successful run, even though no exception was thrown. Only when
// every row's outcome was `applied` does the batch report ok=true.
const hardFailures =
  stats.skipped_no_clone +
  stats.skipped_no_set +
  stats.skipped_no_variant +
  stats.skipped_no_parent +
  stats.errors;

return {
  ok: hardFailures === 0,
  rows: ROWS.length,
  stats,
  errorsSample,
  skipsSample,
};
"""


def render_swap_script_from_writer_rows(
    *,
    page_node_id: str,
    namespace: str,
    writer_rows: list[dict[str, Any]],
) -> str:
    """Render the swap JS from already-aliased writer-shape dicts.

    Used by the shared use_figma batch writer, which has already invoked
    :func:`row_to_writer_dict` to produce per-row dicts.
    """
    return (
        AUDIT_PAGE_SWAP_JS_TEMPLATE.replace("__TARGET_PAGE_ID__", json.dumps(page_node_id))
        .replace("__NAMESPACE__", json.dumps(namespace))
        .replace("__ROWS__", json.dumps(writer_rows, separators=(",", ":"), ensure_ascii=True))
        .replace("__READ_SPD_CHUNKS_JS__", READ_SPD_CHUNKS_JS)
        .replace("__WRITE_SPD_CHUNKS_JS__", WRITE_SPD_CHUNKS_JS)
        .lstrip()
    )


def render_swap_script(
    *,
    page_node_id: str,
    namespace: str,
    rows: list[SwapRow],
) -> str:
    """Render the swap JS for a list of typed :class:`SwapRow` objects."""
    return render_swap_script_from_writer_rows(
        page_node_id=page_node_id,
        namespace=namespace,
        writer_rows=[_row_to_writer(row) for row in rows],
    )


def row_to_writer_dict(row: SwapRow) -> dict[str, Any]:
    """Public alias for the writer-shape row dict (used by the batch writer)."""
    return _row_to_writer(row)


__all__ = [
    "AUDIT_PAGE_SWAP_JS_TEMPLATE",
    "AUDIT_PAGE_SWAP_SCHEMA_VERSION",
    "SwapManifest",
    "SwapRow",
    "load_swap_manifest",
    "render_swap_script_from_writer_rows",
    "row_to_writer_dict",
    "render_swap_script",
]
