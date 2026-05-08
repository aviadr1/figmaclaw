"""Build and emit Figma token-binding apply batches.

The public CLI lives in :mod:`figmaclaw.commands.apply_tokens`; this module
keeps the data validation and JS generation reusable and testable.

``apply-tokens`` is intentionally the bottom stage of the migration pipeline:
it applies concrete binding fixes. It does not decide policy, designer-review
outcomes, or F16 inheritance preservation. Producers must pass rows that have
already been filtered for clean instance inheritance.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from figmaclaw.figma_utils import write_json_if_changed
from figmaclaw.token_catalog import (
    AUTHORITATIVE_DEFINITION_SOURCES,
    CatalogVariable,
    TokenCatalog,
)

APPLY_TOKENS_SCHEMA_VERSION = 1
APPLY_BATCH_MANIFEST_SCHEMA_VERSION = 1
DEFAULT_NAMESPACE = "linear_git_migration"


class ApplyTokenFix(BaseModel):
    """One concrete Figma variable-binding intent.

    ``node_id`` is the source node id. In audit-page migration mode the emitted
    writer resolves it through the audit page's SharedPluginData idMap; with
    direct-node mode it is applied as-is.
    """

    node_id: str
    property: str
    variable_id: str
    variable_key: str | None = None
    token_name: str | None = None
    source: str
    catalog_source_version: str | None = None
    value: Any | None = None
    paint_index: int = 0


class ApplyTokensManifest(BaseModel):
    """Versioned apply-tokens fix manifest."""

    schema_version: int = APPLY_TOKENS_SCHEMA_VERSION
    file_key: str | None = None
    page_node_id: str | None = None
    fixes: list[ApplyTokenFix] = Field(default_factory=list)


@dataclass(frozen=True)
class Refusal:
    row_index: int
    reason: str
    row: dict[str, Any]


@dataclass(frozen=True)
class PreparedApplyTokens:
    manifest: ApplyTokensManifest
    refusals: list[Refusal]

    @property
    def ok(self) -> bool:
        return not self.refusals


def load_apply_token_input(
    input_path: Path,
    *,
    file_key: str | None,
    page_node_id: str | None,
    catalog: TokenCatalog,
    allow_non_authoritative: bool = False,
    allow_variable_id_fallback: bool = False,
    allow_catalog_source_mismatch: bool = False,
    library_hashes: set[str] | None = None,
) -> PreparedApplyTokens:
    """Load a versioned fix manifest or legacy compact binding rows.

    Supported inputs:
    * ``{"schema_version": 1, "fixes": [...]}`` — the stable #42 schema.
    * ``[{"n": "...", "p": "...", "t": "...", "v": ...}]`` — legacy
      migration ``bindings_for_figma.json`` rows. These are resolved to concrete
      variable IDs/keys through the authoritative catalog.
    """
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "fixes" in payload:
        manifest = ApplyTokensManifest.model_validate(payload)
        if file_key is not None:
            manifest.file_key = file_key
        if page_node_id is not None:
            manifest.page_node_id = page_node_id
        refusals = _validate_manifest_fixes(
            manifest,
            catalog=catalog,
            allow_non_authoritative=allow_non_authoritative,
            allow_variable_id_fallback=allow_variable_id_fallback,
            allow_catalog_source_mismatch=allow_catalog_source_mismatch,
            library_hashes=library_hashes,
        )
        return PreparedApplyTokens(manifest, refusals)

    if isinstance(payload, list):
        if file_key is None:
            raise ValueError("--file is required when resolving compact binding rows")
        if page_node_id is None:
            raise ValueError("--page is required when resolving compact binding rows")
        return _from_compact_rows(
            payload,
            file_key=file_key,
            page_node_id=page_node_id,
            catalog=catalog,
            allow_non_authoritative=allow_non_authoritative,
            allow_variable_id_fallback=allow_variable_id_fallback,
            library_hashes=library_hashes,
        )

    if isinstance(payload, dict) and "frames" in payload:
        raise ValueError(
            "suggest-tokens sidecars are aggregated by value and do not contain concrete "
            "node_id rows. Pass a versioned apply-tokens manifest or compact "
            "bindings_for_figma.json rows."
        )

    raise ValueError("expected a versioned apply-tokens manifest or a JSON list of compact rows")


def _from_compact_rows(
    rows: list[Any],
    *,
    file_key: str,
    page_node_id: str,
    catalog: TokenCatalog,
    allow_non_authoritative: bool,
    allow_variable_id_fallback: bool,
    library_hashes: set[str] | None,
) -> PreparedApplyTokens:
    fixes: list[ApplyTokenFix] = []
    refusals: list[Refusal] = []
    token_index = _catalog_by_token_name(catalog, library_hashes=library_hashes)

    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            refusals.append(Refusal(index, "row_not_object", {"value": raw}))
            continue
        node_id = raw.get("node_id") or raw.get("n")
        prop = raw.get("property") or raw.get("p")
        token = raw.get("token_name") or raw.get("t")
        if not node_id or not prop or not token:
            refusals.append(Refusal(index, "missing_node_property_or_token", dict(raw)))
            continue
        candidates = token_index.get(str(token), [])
        if len(candidates) != 1:
            reason = "token_not_in_catalog" if not candidates else "ambiguous_token_name"
            refusals.append(Refusal(index, reason, dict(raw)))
            continue
        variable_id, variable = candidates[0]
        refusal = _catalog_refusal(
            variable_id,
            variable,
            row=dict(raw),
            row_index=index,
            allow_non_authoritative=allow_non_authoritative,
            allow_variable_id_fallback=allow_variable_id_fallback,
        )
        if refusal is not None:
            refusals.append(refusal)
            continue
        fixes.append(
            ApplyTokenFix(
                node_id=str(node_id),
                property=str(prop),
                value=raw.get("value", raw.get("v")),
                token_name=str(token),
                variable_id=variable_id,
                variable_key=variable.key,
                source=variable.source,
                catalog_source_version=_catalog_source_version(catalog, variable),
                paint_index=int(raw.get("paint_index", 0) or 0),
            )
        )

    return PreparedApplyTokens(
        ApplyTokensManifest(file_key=file_key, page_node_id=page_node_id, fixes=fixes),
        refusals,
    )


def _validate_manifest_fixes(
    manifest: ApplyTokensManifest,
    *,
    catalog: TokenCatalog,
    allow_non_authoritative: bool,
    allow_variable_id_fallback: bool,
    allow_catalog_source_mismatch: bool,
    library_hashes: set[str] | None,
) -> list[Refusal]:
    refusals: list[Refusal] = []
    for index, fix in enumerate(manifest.fixes):
        variable = catalog.variables.get(fix.variable_id)
        row = fix.model_dump(mode="json")
        if variable is None:
            refusals.append(Refusal(index, "variable_not_in_catalog", row))
            continue
        if library_hashes is not None and variable.library_hash not in library_hashes:
            refusals.append(Refusal(index, "variable_outside_library_filter", row))
            continue
        refusal = _catalog_refusal(
            fix.variable_id,
            variable,
            row=row,
            row_index=index,
            allow_non_authoritative=allow_non_authoritative,
            allow_variable_id_fallback=allow_variable_id_fallback,
        )
        if refusal is not None:
            refusals.append(refusal)
            continue
        catalog_source_version = _catalog_source_version(catalog, variable)
        if (
            not allow_catalog_source_mismatch
            and fix.catalog_source_version is not None
            and catalog_source_version is not None
            and fix.catalog_source_version != catalog_source_version
        ):
            refusals.append(Refusal(index, "catalog_source_version_mismatch", row))
            continue
        if fix.variable_key is None and variable.key is not None:
            fix.variable_key = variable.key
        if fix.token_name is None and variable.name is not None:
            fix.token_name = variable.name
        fix.source = variable.source
        fix.catalog_source_version = catalog_source_version
        # Catalog identity wins: it is the current authoritative registry.
        if (
            fix.variable_key is not None
            and variable.key is not None
            and fix.variable_key != variable.key
        ):
            refusals.append(Refusal(index, "variable_key_mismatch", row))
    return refusals


def _catalog_by_token_name(
    catalog: TokenCatalog,
    *,
    library_hashes: set[str] | None,
) -> dict[str, list[tuple[str, CatalogVariable]]]:
    by_name: dict[str, list[tuple[str, CatalogVariable]]] = {}
    for variable_id, variable in catalog.variables.items():
        if library_hashes is not None and variable.library_hash not in library_hashes:
            continue
        if not variable.name:
            continue
        by_name.setdefault(variable.name, []).append((variable_id, variable))
    return by_name


def _catalog_refusal(
    variable_id: str,
    variable: CatalogVariable,
    *,
    row: dict[str, Any],
    row_index: int,
    allow_non_authoritative: bool,
    allow_variable_id_fallback: bool,
) -> Refusal | None:
    if not allow_non_authoritative and variable.source not in AUTHORITATIVE_DEFINITION_SOURCES:
        return Refusal(row_index, "non_authoritative_variable", row)
    bindable_key = variable.key or row.get("variable_key")
    if not allow_variable_id_fallback and not bindable_key:
        return Refusal(row_index, "missing_variable_key", row | {"variable_id": variable_id})
    return None


def _catalog_source_version(catalog: TokenCatalog, variable: CatalogVariable) -> str | None:
    if not variable.library_hash:
        return None
    library = catalog.libraries.get(variable.library_hash)
    return library.source_version if library else None


def apply_plan_report(prepared: PreparedApplyTokens) -> dict[str, Any]:
    """Return a stable report for dry-run and refusal output."""
    refusal_counts = Counter(r.reason for r in prepared.refusals)
    property_counts = Counter(fix.property for fix in prepared.manifest.fixes)
    return {
        "schema_version": APPLY_TOKENS_SCHEMA_VERSION,
        "ok": prepared.ok,
        "file_key": prepared.manifest.file_key,
        "page_node_id": prepared.manifest.page_node_id,
        "fixes": len(prepared.manifest.fixes),
        "refusals": len(prepared.refusals),
        "counts": {
            "properties": dict(sorted(property_counts.items())),
            "refusals": dict(sorted(refusal_counts.items())),
        },
        "refusal_sample": [
            {"row_index": r.row_index, "reason": r.reason, "row": r.row}
            for r in prepared.refusals[:20]
        ],
    }


def refusal_report(prepared: PreparedApplyTokens) -> dict[str, Any]:
    """Return all refused rows for operator iteration."""
    return {
        "schema_version": APPLY_TOKENS_SCHEMA_VERSION,
        "kind": "figmaclaw.apply_tokens.refusals",
        "file_key": prepared.manifest.file_key,
        "page_node_id": prepared.manifest.page_node_id,
        "refusals": [
            {"row_index": r.row_index, "reason": r.reason, "row": r.row} for r in prepared.refusals
        ],
    }


def referenced_catalog_source_file_keys(
    prepared: PreparedApplyTokens,
    catalog: TokenCatalog,
) -> set[str]:
    """Return catalog source file keys used by accepted fixes."""
    file_keys: set[str] = set()
    for fix in prepared.manifest.fixes:
        variable = catalog.variables.get(fix.variable_id)
        if variable is None or not variable.library_hash:
            continue
        library = catalog.libraries.get(variable.library_hash)
        if library is not None and library.source_file_key:
            file_keys.add(library.source_file_key)
    return file_keys


def write_apply_batches(
    prepared: PreparedApplyTokens,
    *,
    batch_dir: Path,
    batch_size: int,
    namespace: str = DEFAULT_NAMESPACE,
    node_map: Literal["shared-plugin-data", "direct"] = "shared-plugin-data",
) -> dict[str, Any]:
    """Write deterministic batch rows, JS files, and a batch manifest."""
    if not prepared.ok:
        raise ValueError("refusing to emit batches while apply-token refusals remain")
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    file_key = prepared.manifest.file_key
    page_node_id = prepared.manifest.page_node_id
    if not file_key:
        raise ValueError("file_key is required")
    if not page_node_id:
        raise ValueError("page_node_id is required")

    batch_dir.mkdir(parents=True, exist_ok=True)
    fixes = prepared.manifest.fixes
    batches: list[dict[str, Any]] = []
    calls: list[dict[str, str]] = []

    for batch_index, start in enumerate(range(0, len(fixes), batch_size), start=1):
        batch_fixes = fixes[start : start + batch_size]
        rows = [_fix_to_writer_row(fix) for fix in batch_fixes]
        rows_path = batch_dir / f"batch-{batch_index:04d}.json"
        js_path = batch_dir / f"batch-{batch_index:04d}.use_figma.js"
        write_json_if_changed(rows_path, rows)
        js = render_apply_tokens_script(
            page_node_id=page_node_id,
            namespace=namespace,
            rows=rows,
            node_map=node_map,
        )
        js_path.write_bytes(js.encode("utf-8"))
        description = f"apply design token bindings batch {batch_index:04d}"
        batches.append(
            {
                "index": batch_index,
                "rows": len(rows),
                "rows_path": rows_path.name,
                "js_path": js_path.name,
                "description": description,
            }
        )
        calls.append({"file_key": file_key, "code": js, "description": description})

    manifest = {
        "schema_version": APPLY_BATCH_MANIFEST_SCHEMA_VERSION,
        "kind": "figmaclaw.apply_tokens.batch_manifest",
        "file_key": file_key,
        "page_node_id": page_node_id,
        "namespace": namespace,
        "node_map": node_map,
        "batch_size": batch_size,
        "total_fixes": len(fixes),
        "batch_count": len(batches),
        "batches": batches,
    }
    write_json_if_changed(batch_dir / "manifest.json", manifest)
    return {"manifest": manifest, "calls": calls}


def _fix_to_writer_row(fix: ApplyTokenFix) -> dict[str, Any]:
    return {
        "node_id": fix.node_id,
        "property": fix.property,
        "variable_id": fix.variable_id,
        "variable_key": fix.variable_key,
        "token_name": fix.token_name,
        "paint_index": fix.paint_index,
        "value": fix.value,
    }


APPLY_TOKENS_JS_TEMPLATE = r"""
// Generated by figmaclaw apply-tokens.
// Run in the Figma Plugin API runtime with the file open in edit mode.
const TARGET_PAGE_ID = __TARGET_PAGE_ID__;
const NAMESPACE = __NAMESPACE__;
const NODE_MAP = __NODE_MAP__;
const ROWS = __ROWS__;

const targetPage = await figma.getNodeByIdAsync(TARGET_PAGE_ID);
if (!targetPage) throw new Error(`target page not found: ${TARGET_PAGE_ID}`);
if (typeof targetPage.loadAsync === "function") await targetPage.loadAsync();

function readSPDChunks(prefix, countKey) {
  const count = Number(targetPage.getSharedPluginData(NAMESPACE, countKey) || "0");
  const parts = [];
  for (let i = 0; i < count; i++) {
    parts.push(targetPage.getSharedPluginData(NAMESPACE, `${prefix}.${i}`) || "");
  }
  return parts.join("");
}

let idMap = {};
if (NODE_MAP === "shared-plugin-data") {
  const rawIdMap = readSPDChunks("idMap", "idMapChunkCount");
  if (!rawIdMap) throw new Error(`missing idMap SharedPluginData in namespace ${NAMESPACE}`);
  idMap = JSON.parse(rawIdMap);
}

const varsByRef = {};
const variableErrors = [];
for (const row of ROWS) {
  const ref = row.variable_key || row.variable_id;
  if (!ref || varsByRef[ref]) continue;
  try {
    if (row.variable_key) {
      varsByRef[ref] = await figma.variables.importVariableByKeyAsync(row.variable_key);
    } else if (row.variable_id) {
      varsByRef[ref] = await figma.variables.getVariableByIdAsync(row.variable_id);
    }
  } catch (err) {
    variableErrors.push({
      token_name: row.token_name,
      variable_id: row.variable_id,
      variable_key: row.variable_key,
      error: String(err && err.message ? err.message : err),
    });
  }
}

const stats = {
  applied: 0,
  already_bound: 0,
  missing_idmap: 0,
  node_not_found: 0,
  missing_variable: 0,
  paint_mixed: 0,
  paint_no_solid: 0,
  unsupported_property: 0,
  errors: 0,
};
const errors = [];

function sameBoundVariable(ref, variable) {
  if (!ref || !variable) return false;
  return ref.id === variable.id || (variable.key && ref.id === variable.key);
}

for (const row of ROWS) {
  const targetNodeId = NODE_MAP === "shared-plugin-data" ? idMap[row.node_id] : row.node_id;
  if (!targetNodeId) { stats.missing_idmap++; continue; }
  const variable = varsByRef[row.variable_key || row.variable_id];
  if (!variable) { stats.missing_variable++; continue; }
  let node;
  try {
    node = await figma.getNodeByIdAsync(targetNodeId);
  } catch (_err) {
    node = null;
  }
  if (!node) { stats.node_not_found++; continue; }

  try {
    if (row.property === "fill" || row.property === "stroke") {
      const paintProp = row.property === "fill" ? "fills" : "strokes";
      const paints = node[paintProp];
      if (paints === figma.mixed) { stats.paint_mixed++; continue; }
      if (!Array.isArray(paints) || paints.length <= row.paint_index) {
        stats.paint_no_solid++;
        continue;
      }
      const currentPaint = paints[row.paint_index];
      if (!currentPaint || currentPaint.type !== "SOLID") {
        stats.paint_no_solid++;
        continue;
      }
      const existing = currentPaint.boundVariables && currentPaint.boundVariables.color;
      if (sameBoundVariable(existing, variable)) {
        stats.already_bound++;
        continue;
      }
      const nextPaint = figma.variables.setBoundVariableForPaint(currentPaint, "color", variable);
      const nextPaints = paints.slice();
      nextPaints[row.paint_index] = nextPaint;
      node[paintProp] = nextPaints;
      stats.applied++;
    } else if (typeof node.setBoundVariable === "function") {
      const existing = node.boundVariables && node.boundVariables[row.property];
      if (sameBoundVariable(existing, variable)) {
        stats.already_bound++;
        continue;
      }
      node.setBoundVariable(row.property, variable);
      stats.applied++;
    } else {
      stats.unsupported_property++;
    }
  } catch (err) {
    stats.errors++;
    if (errors.length < 20) {
      errors.push({
        row,
        targetNodeId,
        error: String(err && err.message ? err.message : err),
      });
    }
  }
}

return {
  ok: stats.errors === 0 && stats.missing_variable === 0 && stats.node_not_found === 0,
  targetPageId: targetPage.id,
  nodeMap: NODE_MAP,
  rows: ROWS.length,
  stats,
  variableErrors,
  errorsSample: errors,
};
"""


def render_apply_tokens_script(
    *,
    page_node_id: str,
    namespace: str,
    rows: list[dict[str, Any]],
    node_map: Literal["shared-plugin-data", "direct"],
) -> str:
    return (
        APPLY_TOKENS_JS_TEMPLATE.replace("__TARGET_PAGE_ID__", json.dumps(page_node_id))
        .replace("__NAMESPACE__", json.dumps(namespace))
        .replace("__NODE_MAP__", json.dumps(node_map))
        .replace("__ROWS__", json.dumps(rows, separators=(",", ":"), ensure_ascii=True))
        .lstrip()
    )
