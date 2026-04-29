"""DS variable catalog — file-scope cache of design-token definitions.

Stored at ``.figma-sync/ds_catalog.json``. Populated authoritatively by the
``figmaclaw variables`` command (from Figma's ``/variables/local`` REST
endpoint, or the Figma MCP plugin runtime when REST variables scope is
unavailable) and supplementally by ``seed_catalog.py``-style CSS imports that
produce ``SEEDED:*`` entries when no authoritative variable reader is available.

**Architectural rule (canon §4 TC-1, §5 D13):** the catalog stores
**definitions**. Per-page sidecar files (``*.tokens.json``) store
**usage**. Page-walk observation may add ``observed_on`` and
``usage_count`` to existing entries but MUST NOT introduce a variable
into the catalog or set its definitional fields (name, values_by_mode,
collection, scopes, code_syntax). Definitions come from Figma's variable
registry, either through REST or the plugin runtime.

Invariants enforced here (canon §4 TC):

* TC-2 — every variable carries full identity (library_hash, collection,
  name, resolved_type, values_by_mode, scopes, code_syntax, alias_of, source).
* TC-3 — every model field has a writer; dead fields would fail the
  source-scan meta test.
* TC-4 — ``values_by_mode`` is mode-keyed, never flattened.
* TC-7 — each library entry carries ``source_version`` (Figma file version
  at fetch time) and ``fetched_at``, so consumers can detect staleness.
* TC-8 — ``save_catalog`` is idempotent: identical content (modulo
  timestamps) does not rewrite the file.
* TC-9 — schema migrations migrate forward in place (``_migrate_v1_to_v2``)
  rather than overwriting silently.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from figmaclaw.figma_api_models import LocalVariablesResponse, VariableEntry
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.figma_utils import write_json_if_changed
from figmaclaw.token_scan import ValidBinding

CATALOG_SCHEMA_VERSION = 2

_TOLERANCE = 0.01

# Property bucket classification for numeric matching.
_COLOR_PROPS = {"fill", "stroke"}
_RADIUS_PROPS = {"cornerRadius"}
_SPACING_PROPS = {"itemSpacing", "paddingLeft", "paddingRight", "paddingTop", "paddingBottom"}
_STROKE_WEIGHT_PROPS = {"strokeWeight"}

# Synthetic library hash for variables defined inside a non-DS Figma file
# (i.e. variables not published to any library). Format: ``local:<file_key>``.
LOCAL_LIBRARY_PREFIX = "local:"

# Default mode id used when the source has no mode information (e.g. SEEDED entries).
DEFAULT_MODE_ID = "_default"
AUTHORITATIVE_DEFINITION_SOURCES = {"figma_api", "figma_mcp"}


def _prop_bucket(prop: str) -> str:
    """Return the matching bucket for a property name."""
    if prop in _COLOR_PROPS:
        return "color"
    if prop in _RADIUS_PROPS:
        return "radius"
    if prop in _SPACING_PROPS:
        return "spacing"
    if prop in _STROKE_WEIGHT_PROPS:
        return "strokeWeight"
    return "other"


# ---------------------------------------------------------------------------
# Schema v2 models
# ---------------------------------------------------------------------------


class CatalogValue(BaseModel):
    """One mode-keyed value of a variable.

    Exactly one of ``hex`` / ``numeric_value`` / ``string_value`` /
    ``bool_value`` / ``alias_of`` is non-null per entry. The discriminator
    is the parent variable's ``resolved_type`` (or the presence of
    ``alias_of`` for VARIABLE_ALIAS values).
    """

    hex: str | None = None
    numeric_value: float | None = None
    string_value: str | None = None
    bool_value: bool | None = None
    alias_of: str | None = None  # VariableID:... — resolves through another variable


class CatalogCollection(BaseModel):
    """A variable collection inside a library.

    Populated from the ``variableCollections`` map in
    /variables/local response. Variables reference their collection by id.
    """

    name: str = ""
    default_mode_id: str | None = None
    variable_ids: list[str] = Field(default_factory=list)


class CatalogLibrary(BaseModel):
    """One library (or synthetic ``local:<file_key>`` group) of variables.

    Per canon TC-7, ``source_version`` and ``fetched_at`` make staleness
    explicit: a consumer comparing the manifest's current file version
    against this can decide whether to refresh before relying on the data.

    Per canon D12, ``name`` is the human-readable identity of the library
    (the Figma file name). No code in figmaclaw tests against a hardcoded
    library hash; classification reads identity from this map.
    """

    name: str = ""
    source_file_key: str | None = None
    fetched_at: str | None = None
    source_version: str | None = None
    source: str | None = None
    modes: dict[str, str] = Field(default_factory=dict)  # mode_id -> human name
    default_mode_id: str | None = None
    collections: dict[str, CatalogCollection] = Field(default_factory=dict)


class CatalogVariable(BaseModel):
    """One design-token (Figma variable) entry.

    Per canon TC-2, every field has a single canonical writer. The ``source``
    enum (``figma_api`` / ``figma_mcp`` / ``seeded:css`` /
    ``seeded:manual`` / ``observed``) drives downstream tool behavior — see
    canon §5 D14.

    ``observed_on`` and ``usage_count`` are the only fields that page-walk
    observation may write to. All other fields are populated from a
    /variables/local response, MCP plugin-runtime export, or a CSS seed.
    """

    # Identity
    library_hash: str | None = None
    collection_id: str | None = None
    name: str | None = None
    resolved_type: str | None = None  # COLOR / FLOAT / STRING / BOOLEAN

    # Values
    values_by_mode: dict[str, CatalogValue] = Field(default_factory=dict)

    # Metadata from REST
    scopes: list[str] = Field(default_factory=list)
    code_syntax: dict[str, str] = Field(default_factory=dict)
    alias_of: str | None = None  # variable-level alias (whole-variable redirection)

    # Provenance
    source: str = "observed"  # "figma_api" | "figma_mcp" | "seeded:*" | "observed"

    # Page-walk usage signal (canon TC-1, D13: usage, NOT definition)
    observed_on: list[str] = Field(default_factory=list)
    usage_count: int = 0


class TokenCatalog(BaseModel):
    """Top-level catalog model — the contents of ``ds_catalog.json``."""

    schema_version: int = CATALOG_SCHEMA_VERSION
    updated_at: str | None = None
    libraries: dict[str, CatalogLibrary] = Field(default_factory=dict)
    variables: dict[str, CatalogVariable] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Path + I/O
# ---------------------------------------------------------------------------


def catalog_path(repo_root: Path) -> Path:
    return repo_root / ".figma-sync" / "ds_catalog.json"


def load_catalog(repo_root: Path) -> TokenCatalog:
    """Load the catalog from disk, migrating v1 → v2 transparently if needed.

    Per canon §4 TC-9 / LW-2: schema migration is migrate-forward, not
    auto-archive (no human data is at risk in the v1 catalog because
    ``suggest-tokens`` has not run in CI to populate ``fix_variable_id``).
    """
    path = catalog_path(repo_root)
    if not path.exists():
        return TokenCatalog()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TokenCatalog()

    schema_version = data.get("schema_version", 1)
    if schema_version < CATALOG_SCHEMA_VERSION:
        data = _migrate_v1_to_v2(data)

    try:
        return TokenCatalog.model_validate(data)
    except Exception:
        # Final safety net — never crash the pull just because the catalog
        # is malformed. Start fresh; canon TC-9 / LW-1 behavior.
        return TokenCatalog()


def save_catalog(catalog: TokenCatalog, repo_root: Path) -> None:
    """Write the catalog, skipping the write when only timestamps would change.

    Canon W-1 / TC-8.
    """
    write_json_if_changed(
        catalog_path(repo_root),
        catalog.model_dump(),
        ignore_keys=frozenset({"updated_at", "fetched_at"}),
    )


def catalog_staleness_errors(
    catalog: TokenCatalog, state: FigmaSyncState, file_key: str
) -> list[str]:
    """Return actionable stale/missing catalog errors for one tracked file.

    Canon CR-2 / TC-7: consumers must not produce suggestions from a stale
    catalog. A current catalog has at least one library entry whose
    ``source_file_key`` matches the sidecar's ``file_key`` and whose
    ``source_version`` equals the manifest's current file version.
    """
    file_entry = state.manifest.files.get(file_key)
    if file_entry is None:
        return [f"{file_key}: not present in .figma-sync/manifest.json"]

    libraries = libraries_for_file(catalog, file_key)
    if not libraries:
        return [
            f"{file_key}: ds_catalog.json has no variables registry for this file; "
            f"run `figmaclaw variables --file-key {file_key}`"
        ]

    stale = [
        lib for lib in libraries if _source_version_is_older(lib.source_version, file_entry.version)
    ]
    if stale:
        versions = ", ".join(sorted({lib.source_version or "missing" for lib in stale}))
        return [
            f"{file_key}: ds_catalog.json is stale for manifest version {file_entry.version} "
            f"(catalog source_version: {versions}); run `figmaclaw variables --file-key {file_key}`"
        ]

    return []


def libraries_for_file(catalog: TokenCatalog, file_key: str) -> list[CatalogLibrary]:
    return [lib for lib in catalog.libraries.values() if lib.source_file_key == file_key]


def library_keys_for_file(catalog: TokenCatalog, file_key: str) -> list[str]:
    return [key for key, lib in catalog.libraries.items() if lib.source_file_key == file_key]


def has_figma_api_definitions_for_file(catalog: TokenCatalog, file_key: str) -> bool:
    library_keys = set(library_keys_for_file(catalog, file_key))
    return any(
        entry.source in AUTHORITATIVE_DEFINITION_SOURCES and entry.library_hash in library_keys
        for entry in catalog.variables.values()
    )


def _source_version_is_older(source_version: str | None, manifest_version: str) -> bool:
    """True when the catalog source version is older than manifest state.

    Figma file versions are numeric strings in practice. If a standalone
    variables refresh observes a version newer than the local manifest, the
    catalog is not stale for token suggestions; the page sync cache is merely
    behind. For non-numeric future formats, fall back to equality because no
    ordering is available.
    """
    if not source_version:
        return True
    if source_version == manifest_version:
        return False
    try:
        return int(source_version) < int(manifest_version)
    except ValueError:
        return source_version != manifest_version


def library_hashes_for_file(catalog: TokenCatalog, file_key: str) -> list[str]:
    return [
        lib_hash
        for lib_hash, lib in catalog.libraries.items()
        if lib.source_file_key == file_key and not lib_hash.startswith(LOCAL_LIBRARY_PREFIX)
    ]


# ---------------------------------------------------------------------------
# Migration v1 → v2
# ---------------------------------------------------------------------------


def _migrate_v1_to_v2(v1_data: dict[str, Any]) -> dict[str, Any]:
    """Convert a v1 catalog dict to v2 shape.

    v1 entries had ``{name, hex, numeric_value, observed_on}`` flat per
    variable. v2 introduces ``libraries`` (empty until first variables
    refresh) and a richer per-variable model. We promote any v1 value
    into ``values_by_mode["_default"]``. ``SEEDED:*`` entries get
    ``source: "seeded:legacy"`` so we keep them; everything else gets
    ``source: "observed"`` (legacy graceful-degradation per canon D14).
    """
    v1_vars: dict[str, Any] = v1_data.get("variables", {})
    v2_vars: dict[str, Any] = {}

    for vid, entry in v1_vars.items():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        hex_val = entry.get("hex")
        numeric = entry.get("numeric_value")
        observed = entry.get("observed_on", [])

        is_seeded = vid.startswith("SEEDED:")
        source = "seeded:legacy" if is_seeded else "observed"

        # Best-effort library_hash extraction from the variable ID.
        library_hash: str | None = None
        if vid.startswith("VariableID:"):
            inner = vid.removeprefix("VariableID:")
            if "/" in inner:
                library_hash = inner.split("/", 1)[0]

        # Promote the single value to the default mode bucket.
        values_by_mode: dict[str, dict[str, Any]] = {}
        if hex_val is not None or numeric is not None:
            values_by_mode[DEFAULT_MODE_ID] = {
                "hex": hex_val,
                "numeric_value": numeric,
            }

        v2_vars[vid] = {
            "library_hash": library_hash,
            "name": name,
            "values_by_mode": values_by_mode,
            "observed_on": list(observed),
            "source": source,
        }

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "updated_at": v1_data.get("updated_at"),
        "libraries": {},  # populated by the variables command
        "variables": v2_vars,
    }


# ---------------------------------------------------------------------------
# Refresh from /variables/local — canon TC-1, TC-5
# ---------------------------------------------------------------------------


def merge_local_variables(
    catalog: TokenCatalog,
    response: LocalVariablesResponse,
    *,
    file_key: str,
    file_name: str,
    file_version: str,
    source: str = "figma_api",
) -> int:
    """Ingest local variable definitions into the catalog.

    Populates a library entry (keyed by the lib_hash derived from variable
    IDs, or ``local:<file_key>`` for unpublished variables) and writes
    full identity for every variable in the response.

    Canon §4 TC-1 (authoritative source), TC-2 (complete identity),
    TC-4 (mode-aware), TC-7 (source_version observable). ``source`` records
    which authoritative reader provided the definitions (``figma_api`` or
    ``figma_mcp``).

    Returns the count of variable entries added or updated.
    """
    fetched_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Library hash: derive from any variable id with a /<lib_hash>/ segment.
    # Fall back to the synthetic "local:<file_key>" key for files that only
    # have unpublished variables (no library hash in their IDs).
    lib_hash = _derive_library_hash(response.meta.variables) or _local_library_key(file_key)

    # Build collection map (dict preserves order in Python 3.7+, which we rely on
    # for deterministic JSON output).
    collections: dict[str, CatalogCollection] = {}
    modes_map: dict[str, str] = {}
    default_mode_id: str | None = None

    for coll_id, coll in response.meta.variableCollections.items():
        collections[coll_id] = CatalogCollection(
            name=coll.name,
            default_mode_id=coll.defaultModeId or None,
            variable_ids=list(coll.variableIds),
        )
        for mode in coll.modes:
            modes_map[mode.modeId] = mode.name
        if not default_mode_id and coll.defaultModeId:
            default_mode_id = coll.defaultModeId

    catalog.libraries[lib_hash] = CatalogLibrary(
        name=file_name,
        source_file_key=file_key,
        fetched_at=fetched_at,
        source_version=file_version,
        source=source,
        modes=modes_map,
        default_mode_id=default_mode_id,
        collections=collections,
    )

    count = 0
    for vid, var_entry in response.meta.variables.items():
        existing = catalog.variables.get(vid)
        # Preserve usage stats accumulated by token_scan; replace definition.
        observed_on = list(existing.observed_on) if existing else []
        usage_count = existing.usage_count if existing else 0

        catalog.variables[vid] = CatalogVariable(
            library_hash=lib_hash,
            collection_id=var_entry.variableCollectionId or None,
            name=var_entry.name,
            resolved_type=var_entry.resolvedType or None,
            values_by_mode=_build_values_by_mode(var_entry),
            scopes=list(var_entry.scopes),
            code_syntax=dict(var_entry.codeSyntax),
            alias_of=None,  # variable-level alias resolution not used by Figma here
            source=source,
            observed_on=observed_on,
            usage_count=usage_count,
        )
        count += 1

    catalog.updated_at = fetched_at
    return count


def mark_local_variables_unavailable(
    catalog: TokenCatalog,
    *,
    file_key: str,
    file_name: str,
    file_version: str,
) -> None:
    """Record that the variables endpoint was checked but unavailable.

    Figma returns 403 when the token lacks Enterprise ``file_variables:read``.
    Per D14, consumers may still rely on seeded catalog entries in that case,
    but CR-2 still needs source-version metadata so a reader can distinguish
    "checked and unavailable" from "stale or never refreshed". If the same
    synthetic local library already has authoritative definitions, preserve
    that hard-won registry and let its older ``source_version`` prove staleness
    instead of replacing modes/defaults with an unavailable marker.
    """
    library_key = _local_library_key(file_key)
    existing = catalog.libraries.get(library_key)
    if existing is not None and existing.source in AUTHORITATIVE_DEFINITION_SOURCES:
        # Do not downgrade a previously authoritative local-variable registry
        # to an unavailable marker. Keeping the older source_version lets
        # consumers/strict proof paths report staleness without losing modes,
        # collections, or the default-mode mapping needed by existing entries.
        return
    if (
        existing is not None
        and existing.name == file_name
        and existing.source_file_key == file_key
        and existing.source_version == file_version
        and existing.source == "unavailable"
    ):
        return

    fetched_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    catalog.libraries[library_key] = CatalogLibrary(
        name=file_name,
        source_file_key=file_key,
        fetched_at=fetched_at,
        source_version=file_version,
        source="unavailable",
    )
    catalog.updated_at = fetched_at


def _derive_library_hash(variables: dict[str, VariableEntry]) -> str | None:
    """Extract the library hash from any variable id of form
    ``VariableID:<lib_hash>/<id>``. Returns None when no variable in the
    response carries a slash-separated library hash (i.e. the file has
    only local unpublished variables)."""
    for vid in variables:
        inner = vid.removeprefix("VariableID:")
        if "/" in inner:
            return inner.split("/", 1)[0]
    return None


def _local_library_key(file_key: str) -> str:
    return f"{LOCAL_LIBRARY_PREFIX}{file_key}"


def _build_values_by_mode(var_entry: VariableEntry) -> dict[str, CatalogValue]:
    """Translate Figma's ``valuesByMode`` shape into the catalog's typed shape.

    For COLOR variables the value is a dict ``{r,g,b,a}``; we convert to
    hex. For FLOAT it's a number, for STRING a string, for BOOLEAN a bool.
    A value of shape ``{"type": "VARIABLE_ALIAS", "id": ...}`` is recorded
    as an alias. Unknown shapes pass through unchanged into the appropriate
    field (graceful degradation; canon LW-1 behavior).
    """
    out: dict[str, CatalogValue] = {}
    for mode_id, raw in var_entry.valuesByMode.items():
        out[mode_id] = _coerce_value(raw, var_entry.resolvedType)
    return out


def _coerce_value(raw: Any, resolved_type: str) -> CatalogValue:
    if isinstance(raw, dict):
        if raw.get("type") == "VARIABLE_ALIAS":
            return CatalogValue(alias_of=raw.get("id"))
        if "r" in raw and "g" in raw and "b" in raw:
            return CatalogValue(hex=_rgba_to_hex(raw))
    if isinstance(raw, bool):
        return CatalogValue(bool_value=raw)
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return CatalogValue(numeric_value=float(raw))
    if isinstance(raw, str):
        return CatalogValue(string_value=raw)
    return CatalogValue()  # unknown shape — empty value, preserved for visibility


def _rgba_to_hex(color: dict[str, float]) -> str:
    r = round(color.get("r", 0.0) * 255)
    g = round(color.get("g", 0.0) * 255)
    b = round(color.get("b", 0.0) * 255)
    return f"#{r:02X}{g:02X}{b:02X}"


# ---------------------------------------------------------------------------
# Page-walk usage merge — canon TC-1, D13 (usage only, never definitions)
# ---------------------------------------------------------------------------


def merge_bindings(catalog: TokenCatalog, bindings: list[ValidBinding]) -> int:
    """Record page-walk usage signals into existing variable entries.

    **Per canon §5 D13: this function only writes USAGE fields**
    (``observed_on``, ``usage_count``). It MUST NOT set definitional
    fields (``name``, ``values_by_mode``, ``scopes``, etc.) — those come
    exclusively from ``merge_local_variables``.

    If a page walk sees a variable that is absent from the catalog, the
    binding is ignored here. The remedy is a file-scope variables refresh,
    not adding an observation-defined catalog entry.

    Returns the count of existing variable entries whose usage changed.
    """
    changed_count = 0
    for b in bindings:
        vid = b.variable_id
        if not vid:
            continue
        entry = catalog.variables.get(vid)
        if entry is None:
            continue

        # Usage signals — canonical writer for these two fields.
        if b.property not in entry.observed_on:
            entry.observed_on.append(b.property)
            changed_count += 1
        entry.usage_count += 1
        changed_count += 1

    catalog.updated_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return changed_count


def _extract_library_hash(vid: str) -> str | None:
    if not vid.startswith("VariableID:"):
        return None
    inner = vid.removeprefix("VariableID:")
    if "/" in inner:
        return inner.split("/", 1)[0]
    return None


# ---------------------------------------------------------------------------
# Suggest-tokens — read path; canon TC, CR-2
# ---------------------------------------------------------------------------


def suggest_for_sidecar(sidecar: dict, catalog: TokenCatalog) -> dict:
    """Enrich a sidecar dict in-place with token suggestions from the catalog.

    Sets ``suggest_status`` ("auto"|"ambiguous"|"no_match") and
    ``candidates`` on each issue. For "auto": also sets
    ``fix_variable_id`` unless already non-null (human-set; canon
    LW-2 / TS-S-5 preserves human edits across runs).

    Match strategy:
      * Color properties — match by hex against any variable's
        default-mode value, OR any mode value (priority: default mode).
      * Numeric properties — match by ``round(value, 4)`` within the
        same property bucket.

    Returns the modified sidecar dict.
    """
    hex_to_vids: dict[str, list[str]] = {}
    numeric_to_vids: dict[tuple[str, str], list[str]] = {}

    for vid, entry in catalog.variables.items():
        # Pick the default-mode value for indexing; fall back to the first
        # mode value if no default is recorded.
        value = _pick_default_value(entry, catalog)
        if value is None:
            continue
        if value.hex:
            hex_to_vids.setdefault(value.hex.upper(), []).append(vid)
        if value.numeric_value is not None:
            for prop in entry.observed_on or _scopes_to_props(entry.scopes):
                bucket = _prop_bucket(prop)
                if bucket == "color":
                    continue
                key = (bucket, _numeric_key(value.numeric_value))
                numeric_to_vids.setdefault(key, []).append(vid)

    auto = ambiguous = no_match = 0

    for frame_data in sidecar.get("frames", {}).values():
        for issue in frame_data.get("issues", []):
            prop = issue.get("property", "")
            bucket = _prop_bucket(prop)

            candidates: list[str] = []

            if bucket == "color":
                hex_val = issue.get("hex")
                if hex_val:
                    candidates = list(hex_to_vids.get(hex_val.upper(), []))
            else:
                raw_val = issue.get("current_value")
                if raw_val is not None and isinstance(raw_val, int | float):
                    key = (bucket, _numeric_key(float(raw_val)))
                    candidates = list(numeric_to_vids.get(key, []))
                    candidates = [
                        vid for vid in candidates if _matches_prop(catalog.variables.get(vid), prop)
                    ]
                    if not candidates:
                        candidates = _find_numeric_approx(float(raw_val), bucket, prop, catalog)

            seen: set[str] = set()
            unique: list[str] = []
            for vid in candidates:
                if vid not in seen:
                    seen.add(vid)
                    unique.append(vid)
            candidates = unique

            if len(candidates) == 1:
                issue["suggest_status"] = "auto"
                issue["candidates"] = candidates
                if not issue.get("fix_variable_id"):
                    issue["fix_variable_id"] = candidates[0]
                auto += 1
            elif len(candidates) > 1:
                issue["suggest_status"] = "ambiguous"
                issue["candidates"] = candidates
                ambiguous += 1
            else:
                issue["suggest_status"] = "no_match"
                issue["candidates"] = []
                no_match += 1

    sidecar["suggested_at"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sidecar


def _pick_default_value(entry: CatalogVariable, catalog: TokenCatalog) -> CatalogValue | None:
    """Pick the variable's value at its library's default mode, or fall
    back to the first mode value, or DEFAULT_MODE_ID, or None."""
    if not entry.values_by_mode:
        return None
    default_mode = None
    if entry.library_hash and entry.library_hash in catalog.libraries:
        default_mode = catalog.libraries[entry.library_hash].default_mode_id
    if default_mode and default_mode in entry.values_by_mode:
        return entry.values_by_mode[default_mode]
    if DEFAULT_MODE_ID in entry.values_by_mode:
        return entry.values_by_mode[DEFAULT_MODE_ID]
    return next(iter(entry.values_by_mode.values()))


def _matches_prop(entry: CatalogVariable | None, prop: str) -> bool:
    if entry is None:
        return False
    if prop in entry.observed_on:
        return True
    # Fall back to scope-derived property compatibility for variables that
    # have a definition but haven't yet been observed on a node.
    return prop in _scopes_to_props(entry.scopes)


# Map Figma variable scopes to the figmaclaw property names they apply to.
# Best-effort: figmaclaw classifies a small set of properties; scopes
# unrelated to those map to no property.
_SCOPE_PROPS: dict[str, set[str]] = {
    "ALL_FILLS": {"fill"},
    "FRAME_FILL": {"fill"},
    "SHAPE_FILL": {"fill"},
    "TEXT_FILL": {"fill"},
    "STROKE_COLOR": {"stroke"},
    "STROKE_FLOAT": {"strokeWeight"},
    "CORNER_RADIUS": {"cornerRadius"},
    "WIDTH_HEIGHT": set(),  # not classified by figmaclaw
    "GAP": {"itemSpacing"},
    "OPACITY": set(),
    "FONT_FAMILY": {"fontFamily"},
    "FONT_SIZE": {"fontSize"},
    "FONT_WEIGHT": {"fontWeight"},
    "FONT_STYLE": set(),
}


def _scopes_to_props(scopes: list[str]) -> set[str]:
    out: set[str] = set()
    for s in scopes:
        out.update(_SCOPE_PROPS.get(s, set()))
    return out


def _numeric_key(value: float) -> str:
    return str(round(value, 4))


def _find_numeric_approx(
    value: float,
    bucket: str,
    prop: str,
    catalog: TokenCatalog,
) -> list[str]:
    """Find catalog variables whose default-mode numeric_value is within
    _TOLERANCE of value, restricted to variables that match the same
    property (via observation or scope-derived compatibility)."""
    del bucket  # bucket compatibility is enforced via _matches_prop below
    results: list[str] = []
    for vid, entry in catalog.variables.items():
        value_entry = _pick_default_value(entry, catalog)
        if value_entry is None or value_entry.numeric_value is None:
            continue
        if abs(value_entry.numeric_value - value) > _TOLERANCE:
            continue
        if not _matches_prop(entry, prop):
            continue
        results.append(vid)
    return results
