"""DS variable catalog — built from valid token bindings observed during pull.

Stored at .figma-sync/ds_catalog.json. Updated after each page pull.
Used by suggest-tokens to match raw values to DS variable IDs.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from pydantic import BaseModel, Field

from figmaclaw.token_scan import ValidBinding

_TOLERANCE = 0.01

# Property bucket classification for numeric matching
_COLOR_PROPS = {"fill", "stroke"}
_RADIUS_PROPS = {"cornerRadius"}
_SPACING_PROPS = {"itemSpacing", "paddingLeft", "paddingRight", "paddingTop", "paddingBottom"}
_STROKE_WEIGHT_PROPS = {"strokeWeight"}


def _prop_bucket(prop: str) -> str:
    if prop in _COLOR_PROPS:
        return "color"
    if prop in _RADIUS_PROPS:
        return "radius"
    if prop in _SPACING_PROPS:
        return "spacing"
    if prop in _STROKE_WEIGHT_PROPS:
        return "strokeWeight"
    return "other"


class CatalogVariable(BaseModel):
    name: str | None = None
    hex: str | None = None
    numeric_value: float | None = None
    observed_on: list[str] = Field(default_factory=list)


class TokenCatalog(BaseModel):
    schema_version: int = 1
    updated_at: str | None = None
    variables: dict[str, CatalogVariable] = Field(default_factory=dict)


def catalog_path(repo_root: Path) -> Path:
    return repo_root / ".figma-sync" / "ds_catalog.json"


def load_catalog(repo_root: Path) -> TokenCatalog:
    path = catalog_path(repo_root)
    if not path.exists():
        return TokenCatalog()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TokenCatalog.model_validate(data)
    except Exception:
        return TokenCatalog()


def save_catalog(catalog: TokenCatalog, repo_root: Path) -> None:
    path = catalog_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(catalog.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def merge_bindings(catalog: TokenCatalog, bindings: list[ValidBinding]) -> int:
    """Merge new bindings into the catalog. Returns count of new variable entries added."""
    new_count = 0
    for b in bindings:
        vid = b.variable_id
        if not vid:
            continue
        if vid not in catalog.variables:
            catalog.variables[vid] = CatalogVariable()
            new_count += 1
        entry = catalog.variables[vid]
        # Update resolved value (last-write-wins; DS mode variation is expected)
        if b.hex is not None:
            entry.hex = b.hex
        if b.numeric_value is not None:
            entry.numeric_value = b.numeric_value
        # Accumulate observed properties
        if b.property not in entry.observed_on:
            entry.observed_on.append(b.property)
    catalog.updated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return new_count


def suggest_for_sidecar(sidecar: dict, catalog: TokenCatalog) -> dict:
    """Enrich a sidecar dict in-place with token suggestions from the catalog.

    Sets suggest_status ("auto"|"ambiguous"|"no_match") and candidates on each issue.
    For "auto": also sets fix_variable_id unless already non-null (human-set).
    Adds top-level suggested_at timestamp.
    Returns the modified sidecar dict.
    """
    # Build reverse lookup maps
    hex_to_vids: dict[str, list[str]] = {}
    # (bucket, value_key) → [vid]; value_key is str(round(v, 4)) for numeric
    numeric_to_vids: dict[tuple[str, str], list[str]] = {}

    for vid, entry in catalog.variables.items():
        if entry.hex:
            hex_to_vids.setdefault(entry.hex.upper(), []).append(vid)
        if entry.numeric_value is not None:
            for prop in entry.observed_on:
                bucket = _prop_bucket(prop)
                if bucket in ("color",):
                    continue  # color vars matched by hex only
                key = (bucket, _numeric_key(entry.numeric_value))
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
                    candidates = hex_to_vids.get(hex_val.upper(), [])
            else:
                raw_val = issue.get("current_value")
                if raw_val is not None and isinstance(raw_val, (int, float)):
                    key = (bucket, _numeric_key(float(raw_val)))
                    candidates = [
                        vid for vid in numeric_to_vids.get(key, [])
                        if prop in (catalog.variables[vid].observed_on if vid in catalog.variables else [])
                    ]
                    # Fallback: try approximate match if exact key misses
                    if not candidates:
                        candidates = _find_numeric_approx(
                            float(raw_val), bucket, prop, catalog
                        )

            # Deduplicate while preserving order
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

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sidecar["suggested_at"] = now

    return sidecar


def _numeric_key(value: float) -> str:
    return str(round(value, 4))


def _find_numeric_approx(
    value: float,
    bucket: str,
    prop: str,
    catalog: TokenCatalog,
) -> list[str]:
    """Find catalog variables whose numeric_value is within _TOLERANCE of value,
    restricted to variables observed on properties in the same bucket."""
    results = []
    for vid, entry in catalog.variables.items():
        if entry.numeric_value is None:
            continue
        if abs(entry.numeric_value - value) > _TOLERANCE:
            continue
        # Must have been observed on a property in the same bucket AND same prop
        if prop not in entry.observed_on:
            continue
        results.append(vid)
    return results
