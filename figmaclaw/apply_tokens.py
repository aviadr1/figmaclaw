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

from figmaclaw.figma_js import READ_SPD_CHUNKS_JS
from figmaclaw.token_catalog import (
    AUTHORITATIVE_DEFINITION_SOURCES,
    CatalogVariable,
    TokenCatalog,
)
from figmaclaw.use_figma_batches import write_use_figma_batches

APPLY_TOKENS_SCHEMA_VERSION = 1
APPLY_BATCH_MANIFEST_SCHEMA_VERSION = 1
DEFAULT_NAMESPACE = "linear_git_migration"

# F48: aborting the phase after this many rows hit the same signature
# (e.g. unloadable_font:Boldonse Bold). The default 5 is small enough to
# fire on the first batch's worst class and large enough that a one-off
# transient (network, permission flicker) never trips it.
DEFAULT_SIGNATURE_ABORT_THRESHOLD = 5

# Exit code for "operator action required" — distinct from the generic
# refusal exit so CI / wrappers can route the failure to a human queue.
# Borrows the EX_CONFIG (78) Unix sysexits convention for "configuration
# error" since the operator action (upload font, grant permission, …) is
# always external/configuration.
EXIT_OPERATOR_ACTION_REQUIRED = 78


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
    paint_index: int = Field(default=0, ge=0)


class ApplyTokensManifest(BaseModel):
    """Versioned apply-tokens fix manifest."""

    schema_version: int = APPLY_TOKENS_SCHEMA_VERSION
    file_key: str | None = None
    page_node_id: str | None = None
    fixes: list[ApplyTokenFix] = Field(default_factory=list)


class OperatorAction(BaseModel):
    """F36-shaped block surfaced when a batch aborts on a class-level signature.

    ``signature`` is the dominant abort across all batches in this run;
    ``additional_signatures`` carries any other batches' aborts in
    ``[{"signature": ..., "count": ...}]`` shape so the operator sees the
    full picture instead of being told only about the alphabetically-first
    failure. (Promoted from an ad-hoc dict so downstream JSON consumers can
    rely on the shape — per CLAUDE.md "use pydantic, not dataclass".)
    """

    signature: str
    count: int = Field(ge=0)
    sample_rows: list[str] = Field(default_factory=list)
    instruction: str = ""
    additional_signatures: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class Refusal:
    row_index: int
    reason: str
    row: dict[str, Any]


@dataclass(frozen=True)
class PreparedApplyTokens:
    manifest: ApplyTokensManifest
    refusals: list[Refusal]
    accepted_fix_indices: frozenset[int] | None = None

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
        if manifest.schema_version != APPLY_TOKENS_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported apply-tokens schema_version {manifest.schema_version}; "
                f"expected {APPLY_TOKENS_SCHEMA_VERSION}"
            )
        _merge_manifest_target(manifest, file_key=file_key, page_node_id=page_node_id)
        refusals = _validate_manifest_fixes(
            manifest,
            catalog=catalog,
            allow_non_authoritative=allow_non_authoritative,
            allow_variable_id_fallback=allow_variable_id_fallback,
            allow_catalog_source_mismatch=allow_catalog_source_mismatch,
            library_hashes=library_hashes,
        )
        refused_indices = frozenset(refusal.row_index for refusal in refusals)
        accepted_indices = frozenset(
            index for index in range(len(manifest.fixes)) if index not in refused_indices
        )
        return PreparedApplyTokens(manifest, refusals, accepted_indices)

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


# Canonical → accepted-aliases mapping for compact-row schema. The lint
# uses this both to detect unrecognised keys and to tell the author which
# canonical fields are missing — issue #167 review finding #5.
_COMPACT_ROW_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "node_id": frozenset({"node_id", "n"}),
    "property": frozenset({"property", "p"}),
    "token_name": frozenset({"token_name", "t"}),
}
_COMPACT_ROW_OPTIONAL_KEYS: frozenset[str] = frozenset(
    {"value", "v", "variable_key", "paint_index"}
)
_COMPACT_ROW_RECOGNISED_KEYS: frozenset[str] = (
    frozenset(key for aliases in _COMPACT_ROW_FIELD_ALIASES.values() for key in aliases)
    | _COMPACT_ROW_OPTIONAL_KEYS
)


def _unrecognised_compact_row_fields(raw: dict[str, Any]) -> list[str]:
    return sorted(key for key in raw if key not in _COMPACT_ROW_RECOGNISED_KEYS)


def _missing_compact_row_canonical_fields(raw: dict[str, Any]) -> list[str]:
    """Return canonical field names whose accepted aliases are all absent."""
    keys = set(raw)
    return [
        canonical
        for canonical, aliases in _COMPACT_ROW_FIELD_ALIASES.items()
        if not (aliases & keys)
    ]


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
            unrecognised = _unrecognised_compact_row_fields(raw)
            missing = _missing_compact_row_canonical_fields(raw)
            payload = dict(raw)
            # Always surface BOTH diagnostics — the dominant footgun is a
            # row that has unknown keys AND is missing the canonical ones.
            # Reporting only one forces the author to debug iteratively.
            if unrecognised:
                payload["unrecognised_compact_row_fields"] = unrecognised
            if missing:
                payload["missing_canonical_fields"] = missing
            reason = (
                "unrecognised_compact_row_fields"
                if unrecognised
                else "missing_node_property_or_token"
            )
            refusals.append(Refusal(index, reason, payload))
            continue
        paint_index = _parse_paint_index(raw, index)
        if isinstance(paint_index, Refusal):
            refusals.append(paint_index)
            continue
        token_str = str(token)
        candidates = token_index.get(token_str, [])
        # The legacy migration shape sometimes prefixes token names with the
        # library name (e.g. "tapin:fg/inverse"). Strip a single leading
        # `<lib>:` segment and retry — and if THAT resolves, surface the
        # transparent fixup so the author can update their resolver.
        stripped_token: str | None = None
        if not candidates and ":" in token_str:
            stripped_token = token_str.split(":", 1)[1]
            candidates = token_index.get(stripped_token, [])
        if len(candidates) != 1:
            reason = "token_not_in_catalog" if not candidates else "ambiguous_token_name"
            payload = dict(raw)
            if ":" in token_str:
                payload["did_you_mean_token_name"] = token_str.split(":", 1)[1]
                payload["hint"] = (
                    f"token name {token_str!r} carries a `<library>:` prefix that "
                    f"is not in the catalog; use the bare token name and pass "
                    f"`--library` to scope resolution"
                )
            refusals.append(Refusal(index, reason, payload))
            continue
        # Use the catalog-resolved bare name even if the input row carried a
        # prefix — the emitted manifest stays identity-stable across re-runs.
        token_str = stripped_token or token_str
        variable_id, variable = candidates[0]
        refusal_row = dict(raw)
        # If the prefix-strip retry was what made resolution succeed, carry
        # the hint into any downstream refusal so the operator can see WHY
        # the row resolved differently than the input shape suggested.
        # (#167 review-3 finding #3.)
        if stripped_token is not None:
            refusal_row["did_you_mean_token_name"] = stripped_token
        refusal = _catalog_refusal(
            variable_id,
            variable,
            row=refusal_row,
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
                token_name=token_str,
                variable_id=variable_id,
                variable_key=variable.key or raw.get("variable_key"),
                source=variable.source,
                catalog_source_version=_catalog_source_version(catalog, variable),
                paint_index=paint_index,
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


def _merge_manifest_target(
    manifest: ApplyTokensManifest,
    *,
    file_key: str | None,
    page_node_id: str | None,
) -> None:
    if file_key is not None:
        if manifest.file_key is not None and manifest.file_key != file_key:
            raise ValueError(
                f"manifest file_key {manifest.file_key!r} does not match --file {file_key!r}"
            )
        manifest.file_key = file_key
    if page_node_id is not None:
        if manifest.page_node_id is not None and manifest.page_node_id != page_node_id:
            raise ValueError(
                "manifest page_node_id "
                f"{manifest.page_node_id!r} does not match --page {page_node_id!r}"
            )
        manifest.page_node_id = page_node_id


def _parse_paint_index(raw: dict[str, Any], row_index: int) -> int | Refusal:
    value = raw.get("paint_index", 0)
    if value in (None, ""):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return Refusal(row_index, "invalid_paint_index", dict(raw))
    if parsed < 0:
        return Refusal(row_index, "invalid_paint_index", dict(raw))
    return parsed


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
    accepted_fixes = [
        fix
        for index, fix in enumerate(prepared.manifest.fixes)
        if prepared.accepted_fix_indices is None or index in prepared.accepted_fix_indices
    ]
    property_counts = Counter(fix.property for fix in accepted_fixes)
    input_rows = (
        len(prepared.manifest.fixes)
        if prepared.accepted_fix_indices is not None
        else len(prepared.manifest.fixes) + len(prepared.refusals)
    )
    return {
        "schema_version": APPLY_TOKENS_SCHEMA_VERSION,
        "ok": prepared.ok,
        "file_key": prepared.manifest.file_key,
        "page_node_id": prepared.manifest.page_node_id,
        "input_rows": input_rows,
        "fixes": len(accepted_fixes),
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
    for index, fix in enumerate(prepared.manifest.fixes):
        if prepared.accepted_fix_indices is not None and index not in prepared.accepted_fix_indices:
            continue
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
    catalog: TokenCatalog | None = None,
    library_hashes: set[str] | None = None,
    signature_abort_threshold: int = DEFAULT_SIGNATURE_ABORT_THRESHOLD,
) -> dict[str, Any]:
    """Write deterministic batch rows, JS files, and a batch manifest.

    When *catalog* is provided, every emitted batch JS receives a
    ``dsCatalogKeyByName`` map derived from authoritative catalog
    variables. The runtime uses it as a last-ditch fallback when both
    ``importVariableByKeyAsync(row.variable_key)`` and
    ``getVariableByIdAsync(row.variable_id)`` come up empty — which
    happens for published DS variables that the working file has never
    imported into its local cache. (Issue #168 / F41.)
    """
    if not prepared.ok:
        raise ValueError("refusing to emit batches while apply-token refusals remain")
    file_key = prepared.manifest.file_key
    page_node_id = prepared.manifest.page_node_id
    if not file_key:
        raise ValueError("file_key is required")
    if not page_node_id:
        raise ValueError("page_node_id is required")

    fixes = prepared.manifest.fixes
    catalog_key_by_token_name = (
        _catalog_key_by_token_name(catalog, library_hashes=library_hashes)
        if catalog is not None
        else {}
    )
    return write_use_figma_batches(
        fixes,
        batch_dir=batch_dir,
        batch_size=batch_size,
        file_name_prefix="batch",
        file_key=file_key,
        row_to_dict=_fix_to_writer_row,
        render_js=lambda rows: render_apply_tokens_script(
            page_node_id=page_node_id,
            namespace=namespace,
            rows=rows,
            node_map=node_map,
            catalog_key_by_token_name=catalog_key_by_token_name,
            signature_abort_threshold=signature_abort_threshold,
        ),
        description_prefix="apply design token bindings batch",
        manifest_extras={
            "schema_version": APPLY_BATCH_MANIFEST_SCHEMA_VERSION,
            "kind": "figmaclaw.apply_tokens.batch_manifest",
            "file_key": file_key,
            "page_node_id": page_node_id,
            "namespace": namespace,
            "node_map": node_map,
            "signature_abort_threshold": signature_abort_threshold,
            # Apply-tokens has historically called the row-count `total_fixes`;
            # keep the key so existing batch-manifest consumers don't break.
            # The shared writer also adds `total_rows` for the new convention.
            "total_fixes": len(fixes),
        },
    )


_SIGNATURE_CLASS_ACTIONS: dict[str, str] = {
    "read_only_file": (
        "the MCP session lacks edit access to the target file; switch to a "
        "session whose user has Editor or Owner permission."
    ),
    "missing_variable_key": (
        "an upstream resolver emitted variable_id without a publishable key. "
        "Re-run `figmaclaw variables` to refresh the catalog and re-build "
        "the bindings manifest."
    ),
    "variable_not_found": (
        "variable could not be resolved on the target file. Confirm the "
        "catalog has its publishable key and that the row carries "
        "`token_name` so the F41 import-by-key fallback fires."
    ),
}


def operator_action_for_signature(signature: str) -> str:
    """Return an F36-style operator-actionable hint for a runtime signature.

    Signatures emitted by the JS runtime's ``classifyError`` come in two
    shapes — bare class (``read_only_file``) or ``class:identifier``
    (``unloadable_font:Boldonse Bold``, ``missing_variable_key:VariableID:libabc/1:1``).
    We split on the FIRST colon so the identifier preserves any inner
    colons (e.g. ``VariableID:libabc/1:1`` keeps ``libabc/1:1`` intact).

    Per-class hints:

    * ``unloadable_font:<Name>`` — Figma MCP plugin runtime can't load
      this font even when installed locally. Surface the org-upload
      escape valve.
    * ``read_only_file`` — the connected MCP session lacks edit access
      to the target file.
    * ``missing_variable_key[:<id>]`` — the manifest carries a variable
      id without a publishable key.
    * ``variable_not_found[:<token>]`` — neither the row's id nor the
      catalog name-fallback could resolve the variable.
    """
    if not signature:
        return ""
    cls, _, identifier = signature.partition(":")
    if cls == "unloadable_font" and identifier:
        return (
            f"font {identifier!r} cannot load in the Figma MCP plugin "
            "runtime. Either (a) org-upload the font to your Figma admin → "
            "Fonts page, or (b) ask the DS owner to add a fallback "
            "typography mode binding fontFamily to a runtime-available "
            "font (e.g. Inter)."
        )
    if cls == "variable_not_found" and identifier:
        return (
            f"variable {identifier!r} could not be resolved on the target "
            "file. Confirm the catalog has its publishable key and that "
            "the row carries `token_name` so the F41 import-by-key "
            "fallback fires."
        )
    if cls == "missing_variable_key" and identifier:
        return (
            f"variable id {identifier!r} has no publishable key. Re-run "
            "`figmaclaw variables` to refresh the catalog and re-build "
            "the bindings manifest."
        )
    return _SIGNATURE_CLASS_ACTIONS.get(cls, "")


def _catalog_key_by_token_name(
    catalog: TokenCatalog,
    *,
    library_hashes: set[str] | None = None,
) -> dict[str, str]:
    """Map authoritative variable token-name → publishable key.

    Only AUTHORITATIVE_DEFINITION_SOURCES variables are included so the
    runtime never tries to import a manually-edited or proxied entry.
    When *library_hashes* is provided, the map is restricted to those
    libraries — same filter the operator uses on the CLI. This prevents
    silent collisions when two libraries publish the same token name with
    different keys (e.g. legacy DS + new DS both shipping ``fg/inverse``):
    the resolver picks the user's intended library by construction.

    If duplicate (name, key) pairs survive the filter — i.e. two distinct
    keys for the same token name — we raise rather than silently picking
    the first one Python iterates over. The operator should pass a
    tighter ``--library`` filter to disambiguate.
    """
    name_map: dict[str, str] = {}
    duplicates: dict[str, set[str]] = {}
    for variable in catalog.variables.values():
        if variable.source not in AUTHORITATIVE_DEFINITION_SOURCES:
            continue
        if library_hashes is not None and variable.library_hash not in library_hashes:
            continue
        if not variable.name or not variable.key:
            continue
        existing = name_map.get(variable.name)
        if existing is None:
            name_map[variable.name] = variable.key
        elif existing != variable.key:
            duplicates.setdefault(variable.name, {existing}).add(variable.key)
    if duplicates:
        offenders = ", ".join(
            f"{name}: {sorted(keys)}" for name, keys in sorted(duplicates.items())
        )
        raise ValueError(
            "catalog contains multiple publishable keys for the same token "
            f"name ({offenders}); pass `--library` to scope the resolver"
        )
    return name_map


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
// Catalog map for the F41 fallback: token-name → publishable key. The
// runtime calls `importVariableByKeyAsync(key)` when the row's own
// variable_id/variable_key fail, which covers published DS variables the
// working file has never imported. Empty when no catalog is plumbed.
const CATALOG_KEY_BY_TOKEN_NAME = __CATALOG_KEY_BY_TOKEN_NAME__;
// F48: abort the phase after this many rows hit the same signature.
const SIGNATURE_ABORT_THRESHOLD = __SIGNATURE_ABORT_THRESHOLD__;

const targetPage = await figma.getNodeByIdAsync(TARGET_PAGE_ID);
if (!targetPage) throw new Error(`target page not found: ${TARGET_PAGE_ID}`);
if (typeof targetPage.loadAsync === "function") await targetPage.loadAsync();

__READ_SPD_CHUNKS_JS__

let idMap = {};
if (NODE_MAP === "shared-plugin-data") {
  const rawIdMap = readSPDChunks("idMap", "idMapChunkCount");
  if (!rawIdMap) throw new Error(`missing idMap SharedPluginData in namespace ${NAMESPACE}`);
  idMap = JSON.parse(rawIdMap);
}

// F48: aggregate identical-cause failures into one block. Each call to
// `recordSignature(sig, rowId)` increments the count and pushes up to 3
// sample row ids. When the count crosses SIGNATURE_ABORT_THRESHOLD we
// flag `signatureAbort` so the per-row loop stops processing additional
// rows. The caller (CLI) then surfaces ONE F36 operator-actionable
// block instead of N identical lines in the report.
const errorSignatures = {};
let signatureAbort = null;
function recordSignature(sig, rowId) {
  if (!sig) return;
  const entry = errorSignatures[sig] || { count: 0, sample_rows: [] };
  entry.count++;
  if (entry.sample_rows.length < 3 && rowId !== undefined) entry.sample_rows.push(rowId);
  errorSignatures[sig] = entry;
  if (!signatureAbort && entry.count >= SIGNATURE_ABORT_THRESHOLD) {
    signatureAbort = { signature: sig, count: entry.count, sample_rows: entry.sample_rows.slice() };
  }
}

// F36-friendly signature normalizer. Maps a free-form runtime error into
// a class-level identifier so 263 × "font Boldonse Bold not loaded" all
// collapse to one "unloadable_font:Boldonse Bold" signature.
//
// Every signature has the shape `class:identifier` — the identifier
// makes the F36 hint specific (which font, which variable) instead of
// generic. classes without a natural identifier (read_only_file) are
// passed through bare. The CLI's `operator_action_for_signature` and
// the runtime's `recordSignature` callsites must agree on the format,
// so changing the regex set requires updating both. (#170 review.)
function classifyError(message, contextRow) {
  if (!message) return null;
  const text = String(message);
  let m;
  m = text.match(/font (.+?) (?:not loaded|could not be loaded)/i);
  if (m) return "unloadable_font:" + m[1].trim();
  m = text.match(/loadFontAsync.+?for (.+?)$/);
  if (m) return "unloadable_font:" + m[1].trim();
  if (/read[- ]only|permission denied|cannot edit/i.test(text)) return "read_only_file";
  if (/missing variable key/i.test(text)) {
    // Use the row's variable_id when present so the operator sees WHICH
    // resolver entry needs a publishable key, not just the generic class.
    const id = contextRow && contextRow.variable_id ? ":" + contextRow.variable_id : "";
    return "missing_variable_key" + id;
  }
  if (/cannot find variable|variable not found/i.test(text)) {
    const tok = contextRow && contextRow.token_name ? ":" + contextRow.token_name : "";
    return "variable_not_found" + tok;
  }
  return null;
}

const varsByRef = {};
const variableErrors = [];
// Resolution order is significant: the F41 catalog-name fallback fires
// LAST so it never overrides a row's explicit variable_key/id. Each
// candidate's `kind` is recorded in any error so the operator sees
// which path the runtime tried before giving up.
async function resolveVariable(row) {
  const candidates = [];
  if (row.variable_key) {
    candidates.push({
      kind: "variable_key",
      load: () => figma.variables.importVariableByKeyAsync(row.variable_key),
    });
  }
  if (row.variable_id) {
    candidates.push({
      kind: "variable_id",
      load: () => figma.variables.getVariableByIdAsync(row.variable_id),
    });
  }
  if (row.token_name && CATALOG_KEY_BY_TOKEN_NAME[row.token_name]) {
    const catalogKey = CATALOG_KEY_BY_TOKEN_NAME[row.token_name];
    candidates.push({
      kind: "catalog_token_name",
      catalogKey,
      load: () => figma.variables.importVariableByKeyAsync(catalogKey),
    });
  }
  const tried = [];
  for (const candidate of candidates) {
    try {
      const resolved = await candidate.load();
      if (resolved) return { resolved, kind: candidate.kind };
    } catch (err) {
      tried.push({
        kind: candidate.kind,
        catalogKey: candidate.catalogKey,
        error: String(err && err.message ? err.message : err),
      });
    }
  }
  return { resolved: null, kind: null, tried };
}

for (const row of ROWS) {
  const ref = row.variable_key || row.variable_id;
  if (!ref || varsByRef[ref]) continue;
  const outcome = await resolveVariable(row);
  if (outcome.resolved) {
    varsByRef[ref] = outcome.resolved;
    continue;
  }
  if (outcome.tried && outcome.tried.length > 0) {
    variableErrors.push({
      token_name: row.token_name,
      variable_id: row.variable_id,
      variable_key: row.variable_key,
      tried: outcome.tried,
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
  aborted_by_signature: 0,
};
const errors = [];

function sameBoundVariable(ref, variable) {
  if (!ref || !variable) return false;
  return ref.id === variable.id || (variable.key && ref.id === variable.key);
}

for (const row of ROWS) {
  if (signatureAbort) {
    stats.aborted_by_signature++;
    continue;
  }
  const targetNodeId = NODE_MAP === "shared-plugin-data" ? idMap[row.node_id] : row.node_id;
  if (!targetNodeId) { stats.missing_idmap++; continue; }
  const variable = varsByRef[row.variable_key || row.variable_id];
  if (!variable) {
    stats.missing_variable++;
    if (row.token_name) recordSignature("variable_not_found:" + row.token_name, row.node_id);
    continue;
  }
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
    const message = String(err && err.message ? err.message : err);
    const sig = classifyError(message, row);
    if (sig) recordSignature(sig, row.node_id);
    if (errors.length < 20) {
      errors.push({
        row,
        targetNodeId,
        error: message,
        signature: sig,
      });
    }
  }
}

const hardFailures =
  stats.missing_idmap +
  stats.node_not_found +
  stats.missing_variable +
  stats.paint_mixed +
  stats.paint_no_solid +
  stats.unsupported_property +
  stats.errors +
  stats.aborted_by_signature;

// We never throw here on hardFailures > 0. A throw inside the Figma plugin
// runtime rolls back the entire transaction, so a single bad row in a batch
// of N would atomically revert the (N - 1) successful per-row writes that
// already ran. Rows are independent — each one wraps its own work in a
// try/catch and increments per-reason counters above. Returning the summary
// lets the caller decide whether to fail the run based on aggregate stats.
// `hardFailures` already includes `aborted_by_signature`, so when the
// loop short-circuits on signatureAbort the `aborted_by_signature` count
// alone makes `ok` false. Don't add a redundant `!signatureAbort` here —
// keeping a single source of truth means a future refactor can't leave
// the two checks subtly out of step.
const summary = {
  ok: hardFailures === 0,
  targetPageId: targetPage.id,
  nodeMap: NODE_MAP,
  rows: ROWS.length,
  stats,
  variableErrors,
  errorsSample: errors,
  errorSignatures,
  signatureAbort,
};

return summary;
"""


def render_apply_tokens_script(
    *,
    page_node_id: str,
    namespace: str,
    rows: list[dict[str, Any]],
    node_map: Literal["shared-plugin-data", "direct"],
    catalog_key_by_token_name: dict[str, str] | None = None,
    signature_abort_threshold: int = DEFAULT_SIGNATURE_ABORT_THRESHOLD,
) -> str:
    catalog_map = catalog_key_by_token_name or {}
    return (
        APPLY_TOKENS_JS_TEMPLATE.replace("__TARGET_PAGE_ID__", json.dumps(page_node_id))
        .replace("__NAMESPACE__", json.dumps(namespace))
        .replace("__NODE_MAP__", json.dumps(node_map))
        .replace("__ROWS__", json.dumps(rows, separators=(",", ":"), ensure_ascii=True))
        .replace(
            "__CATALOG_KEY_BY_TOKEN_NAME__",
            json.dumps(catalog_map, separators=(",", ":"), ensure_ascii=True),
        )
        .replace("__SIGNATURE_ABORT_THRESHOLD__", json.dumps(int(signature_abort_threshold)))
        .replace("__READ_SPD_CHUNKS_JS__", READ_SPD_CHUNKS_JS)
        .lstrip()
    )
