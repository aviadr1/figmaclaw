"""Read-only audit helpers for design-system migration checks.

These helpers intentionally operate on Figma REST node trees and explicit
operator-provided migration artifacts. They do not read or write figmaclaw page
markdown bodies.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from figmaclaw.commands.census import load_census_registry
from figmaclaw.token_catalog import AUTHORITATIVE_DEFINITION_SOURCES, TokenCatalog
from figmaclaw.token_scan import bound_variable_id, paint_is_variable_bound, rgb_to_hex


class AuditFinding(BaseModel):
    """A single read-only audit finding."""

    status: str
    source_id: str | None = None
    clone_id: str | None = None
    node_id: str | None = None
    node_name: str | None = None
    node_type: str | None = None
    property: str | None = None
    token: str | None = None
    value: Any = None
    hex: str | None = None
    in_instance: bool | None = None
    path: list[str] = Field(default_factory=list)
    message: str | None = None


class AuditCheckReport(BaseModel):
    """Result for audit-page check."""

    audit_page_id: str
    manifest_rows: int
    ok: bool
    counts: dict[str, int]
    misses: list[AuditFinding]
    limitation: str = (
        "This check proves whether the target clone property is variable-bound at all. "
        "It does not prove exact token identity unless the input manifest carries "
        "Figma variable IDs and a future verifier compares those IDs."
    )


class AuditDiagnoseReport(BaseModel):
    """Result for audit-page diagnose."""

    audit_page_id: str
    ok: bool
    bound_paints: int
    unbound_paints: int
    unique_unbound_hex: int
    counts: dict[str, int]
    old_palette: dict[str, str]
    new_palette: dict[str, str]
    findings: list[AuditFinding]
    limitation: str = (
        "Palette classifications are advisory. Without explicit palette inputs, "
        "unbound colors are reported as unclassified_literal."
    )


TARGET_STATUSES = {
    "replace_with_new_component",
    "compose_from_primitives",
    "designer_audit_required",
    "discard_on_parent_swap",
}
SWAP_STRATEGIES = {
    "create-instance-and-translate",
    "swap-with-translation",
    "swap-direct",
    "none",
}
PARENT_HANDLING = {
    "leave-as-instance",
    "detach-then-swap-inners",
    "compose-from-primitives",
}
EXPECTED_TYPES = {"COMPONENT_SET", "COMPONENT", "FRAME", None}
VALIDATION_BOOLS = {
    "assert_target_type",
    "assert_name_matches",
    "assert_property_keys",
    "assert_variant_axes",
}


class PipelineLintReport(BaseModel):
    """Result for audit-pipeline lint."""

    component_map: str
    ok: bool
    rule_count: int
    counts: dict[str, int]
    findings: list[AuditFinding]
    target_registry_state: str


def walk_nodes(node: dict[str, Any], path: list[str] | None = None) -> Iterable[dict[str, Any]]:
    """Yield every node in a Figma REST tree, including INSTANCE descendants."""
    yield node
    for child in node.get("children") or []:
        yield from walk_nodes(child, [*(path or []), str(node.get("name", ""))])


def walk_nodes_with_context(
    node: dict[str, Any],
    *,
    ancestors: list[dict[str, Any]] | None = None,
    inside_instance: bool = False,
) -> Iterable[tuple[dict[str, Any], list[dict[str, Any]], bool]]:
    """Yield nodes with ancestor and inside-instance context."""
    ancestors = ancestors or []
    yield node, ancestors, inside_instance
    next_inside = inside_instance or node.get("type") == "INSTANCE"
    next_ancestors = [*ancestors, node]
    for child in node.get("children") or []:
        yield from walk_nodes_with_context(
            child,
            ancestors=next_ancestors,
            inside_instance=next_inside,
        )


def index_nodes(node: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return a node-id index for a Figma REST tree."""
    return {str(n.get("id")): n for n in walk_nodes(node) if n.get("id")}


def paint_bound(node: dict[str, Any], prop: str) -> bool:
    """Return whether a fill/stroke-like property is variable-bound."""
    paints = node.get("fills" if prop == "fill" else "strokes") or []
    for index, paint in enumerate(paints):
        if paint.get("visible") is False:
            continue
        if paint_is_variable_bound(node, prop, index, paint):
            return True
    return False


def scalar_bound(node: dict[str, Any], prop: str) -> bool:
    """Return whether a scalar property is variable-bound."""
    bv = node.get("boundVariables") or {}
    if prop == "cornerRadius" and "rectangleCornerRadii" in bv:
        return True
    return prop in bv and bool(bound_variable_id(bv.get(prop)) or bv.get(prop))


def is_bound(node: dict[str, Any] | None, prop: str) -> bool:
    """Return whether *prop* is variable-bound on *node*."""
    if node is None:
        return False
    if prop in {"fill", "stroke"}:
        return paint_bound(node, prop)
    return scalar_bound(node, prop)


def build_audit_check_report(
    page_node: dict[str, Any],
    *,
    audit_page_id: str,
    manifest_rows: list[dict[str, Any]],
    idmap: dict[str, str],
) -> tuple[AuditCheckReport, list[dict[str, Any]]]:
    """Compare binding intent rows against an audit page tree."""
    audit_nodes = index_nodes(page_node)
    findings: list[AuditFinding] = []
    remaining_manifest: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    for row in manifest_rows:
        source_id = str(row.get("n") or row.get("node_id") or "")
        prop = str(row.get("p") or row.get("property") or "")
        clone_id = idmap.get(source_id)
        node = audit_nodes.get(clone_id or "")

        if not clone_id:
            status = "missing_idmap"
        elif node is None:
            status = "missing_clone_node"
        elif is_bound(node, prop):
            status = "bound"
        else:
            status = "missing_or_literal"

        counts[status] += 1
        if status != "bound":
            remaining_manifest.append(row)
            findings.append(
                AuditFinding(
                    status=status,
                    source_id=source_id,
                    clone_id=clone_id,
                    node_id=node.get("id") if node else None,
                    node_name=node.get("name") if node else None,
                    node_type=node.get("type") if node else None,
                    property=prop,
                    token=row.get("t") or row.get("token"),
                    value=row.get("v") if "v" in row else row.get("value"),
                )
            )

    bad = sum(
        counts.get(status, 0)
        for status in ("missing_idmap", "missing_clone_node", "missing_or_literal")
    )
    return (
        AuditCheckReport(
            audit_page_id=audit_page_id,
            manifest_rows=len(manifest_rows),
            ok=bad == 0,
            counts=dict(counts),
            misses=findings,
        ),
        remaining_manifest,
    )


def paint_hex(paint: dict[str, Any]) -> str | None:
    """Return #RRGGBB for a visible solid paint."""
    if paint.get("type") != "SOLID" or paint.get("visible") is False:
        return None
    color = paint.get("color")
    if not isinstance(color, dict):
        return None
    return rgb_to_hex(color)


def build_audit_diagnose_report(
    page_node: dict[str, Any],
    *,
    audit_page_id: str,
    old_palette: dict[str, str] | None = None,
    new_palette: dict[str, str] | None = None,
) -> AuditDiagnoseReport:
    """Classify unbound paints on an audit page."""
    old_palette = normalize_palette(old_palette or {})
    new_palette = normalize_palette(new_palette or {})
    bound_count = 0
    by_hex: dict[str, list[AuditFinding]] = defaultdict(list)
    counts: Counter[str] = Counter()

    for node, ancestors, inside_instance in walk_nodes_with_context(page_node):
        for prop_name, paints in (
            ("fills", node.get("fills") or []),
            ("strokes", node.get("strokes") or []),
        ):
            node_prop = "fill" if prop_name == "fills" else "stroke"
            for index, paint in enumerate(paints):
                if paint.get("visible") is False:
                    continue
                if paint_is_variable_bound(node, node_prop, index, paint):
                    bound_count += 1
                    continue
                hex_value = paint_hex(paint)
                if hex_value is None:
                    continue
                status = classify_hex(hex_value, old_palette=old_palette, new_palette=new_palette)
                counts[status] += 1
                by_hex[hex_value].append(
                    AuditFinding(
                        status=status,
                        node_id=node.get("id"),
                        node_name=node.get("name"),
                        node_type=node.get("type"),
                        property=node_prop,
                        hex=hex_value,
                        in_instance=inside_instance,
                        path=[str(a.get("name", "")) for a in ancestors],
                        message=palette_message(hex_value, old_palette, new_palette),
                    )
                )

    findings: list[AuditFinding] = []
    for hex_value, rows in sorted(by_hex.items(), key=lambda item: (-len(item[1]), item[0])):
        findings.extend(rows)
        counts[f"hex:{hex_value}"] = len(rows)

    unbound = sum(len(rows) for rows in by_hex.values())
    old_standalone = any(
        finding.status == "old_palette_literal" and finding.in_instance is False
        for finding in findings
    )
    frozen_new = any(finding.status == "new_palette_literal" for finding in findings)
    shared_literal = any(finding.status == "shared_palette_literal" for finding in findings)
    return AuditDiagnoseReport(
        audit_page_id=audit_page_id,
        ok=not old_standalone and not frozen_new and not shared_literal,
        bound_paints=bound_count,
        unbound_paints=unbound,
        unique_unbound_hex=len(by_hex),
        counts=dict(counts),
        old_palette=old_palette,
        new_palette=new_palette,
        findings=findings,
    )


def classify_hex(
    hex_value: str,
    *,
    old_palette: dict[str, str],
    new_palette: dict[str, str],
) -> str:
    """Classify an unbound literal hex using explicit palettes."""
    normalized = hex_value.upper()
    in_old = normalized in old_palette
    in_new = normalized in new_palette
    if in_old and in_new:
        return "shared_palette_literal"
    if in_old:
        return "old_palette_literal"
    if in_new:
        return "new_palette_literal"
    return "unclassified_literal"


def palette_message(
    hex_value: str,
    old_palette: dict[str, str],
    new_palette: dict[str, str],
) -> str | None:
    """Return a label for one palette classification finding."""
    normalized = hex_value.upper()
    old_label = old_palette.get(normalized)
    new_label = new_palette.get(normalized)
    if old_label and new_label:
        return f"old: {old_label}; new: {new_label}"
    return old_label or new_label


def normalize_palette(palette: dict[str, str]) -> dict[str, str]:
    """Normalize palette keys to #RRGGBB uppercase strings."""
    return {
        _normalize_hex(str(key)): str(value)
        for key, value in palette.items()
        if _normalize_hex(str(key))
    }


def _normalize_hex(value: str) -> str:
    value = value.strip().upper()
    if not value:
        return ""
    if not value.startswith("#"):
        value = f"#{value}"
    return value


def load_json_file(path: Path) -> Any:
    """Load JSON from a file with a clear exception boundary for commands."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{path}: could not read JSON file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def load_palette(path: Path | None) -> dict[str, str]:
    """Load an explicit palette mapping from JSON."""
    if path is None:
        return {}
    payload = load_json_file(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: palette must be a JSON object mapping hex to label")
    return {str(key): str(value) for key, value in payload.items()}


def parse_palette_entries(entries: Iterable[str]) -> dict[str, str]:
    """Parse repeatable ``HEX=label`` palette entries from command options."""
    palette: dict[str, str] = {}
    for raw in entries:
        if "=" not in raw:
            raise ValueError(f"palette entry must be HEX=label, got {raw!r}")
        hex_value, label = raw.split("=", 1)
        normalized_hex = _normalize_hex(hex_value)
        label = label.strip()
        if not normalized_hex:
            raise ValueError(f"palette entry has empty hex value: {raw!r}")
        if not label:
            raise ValueError(f"palette entry has empty label: {raw!r}")
        palette[normalized_hex] = label
    return palette


def load_palette_from_ds_catalog(path: Path) -> dict[str, str]:
    """Build a color palette from authoritative color variable definitions."""
    try:
        payload = load_json_file(path)
        catalog = TokenCatalog.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"{path}: could not load ds_catalog.json palette: {exc}") from exc
    palette: dict[str, str] = {}
    for variable_id, variable in catalog.variables.items():
        if variable.source not in AUTHORITATIVE_DEFINITION_SOURCES:
            continue
        if variable.resolved_type != "COLOR":
            continue
        label = variable.name or variable_id
        for value in variable.values_by_mode.values():
            if value.hex:
                palette[value.hex] = label
    return normalize_palette(palette)


def build_pipeline_lint_report(
    component_map_path: Path,
    *,
    census_paths: list[Path] | None = None,
) -> PipelineLintReport:
    """Validate the component migration map and optional target census data."""
    payload = load_json_file(component_map_path)
    findings: list[AuditFinding] = []
    counts: Counter[str] = Counter()

    if not isinstance(payload, dict):
        findings.append(AuditFinding(status="error", message="component map must be a JSON object"))
        return PipelineLintReport(
            component_map=str(component_map_path),
            ok=False,
            rule_count=0,
            counts={"error": 1},
            findings=findings,
            target_registry_state="not_probed",
        )

    if payload.get("schema_version") != 3:
        findings.append(AuditFinding(status="error", message="schema_version must be 3"))

    rules = payload.get("rules")
    if not isinstance(rules, list):
        findings.append(AuditFinding(status="error", message="rules must be a list"))
        rules = []

    census = load_census_component_sets(census_paths or [])
    target_registry_state = "not_probed"
    if census_paths:
        target_registry_state = "probed_empty" if not census else "probed_with_entries"

    for idx, row in enumerate(rules):
        if not isinstance(row, dict):
            findings.append(AuditFinding(status="error", message=f"rules[{idx}] must be object"))
            continue
        errors = validate_component_rule(row, idx)
        for error in errors:
            findings.append(AuditFinding(status="error", message=error))
        findings.extend(validate_rule_against_census(row, idx, census, target_registry_state))

    for finding in findings:
        counts[finding.status] += 1

    return PipelineLintReport(
        component_map=str(component_map_path),
        ok=not any(f.status == "error" for f in findings),
        rule_count=len(rules),
        counts=dict(counts),
        findings=findings,
        target_registry_state=target_registry_state,
    )


def validate_component_rule(row: dict[str, Any], idx: int) -> list[str]:
    """Validate the minimum component_migration_map.v3 row contract."""
    errors: list[str] = []
    prefix = f"rules[{idx}]"
    for key in (
        "old_component_set",
        "old_key",
        "target",
        "swap_strategy",
        "parent_handling",
        "property_translation",
        "validation",
    ):
        if key not in row:
            errors.append(f"{prefix}: missing {key}")
    if errors:
        return errors

    if not isinstance(row["old_component_set"], str) or not row["old_component_set"]:
        errors.append(f"{prefix}.old_component_set must be non-empty string")
    if not isinstance(row["old_key"], str) or not row["old_key"]:
        errors.append(f"{prefix}.old_key must be non-empty string")

    target = row["target"]
    if not isinstance(target, dict):
        errors.append(f"{prefix}.target must be object")
        target = {}
    status = target.get("status")
    if status not in TARGET_STATUSES:
        errors.append(f"{prefix}.target.status invalid: {status!r}")
    expected_type = target.get("expected_type")
    if expected_type not in EXPECTED_TYPES:
        errors.append(f"{prefix}.target.expected_type invalid: {expected_type!r}")
    if status == "replace_with_new_component":
        if not target.get("new_key"):
            errors.append(f"{prefix}.target.new_key required for replace_with_new_component")
        if expected_type != "COMPONENT_SET":
            errors.append(
                f"{prefix}.target.expected_type must be COMPONENT_SET for replace_with_new_component"
            )
        if not target.get("expected_new_name"):
            errors.append(
                f"{prefix}.target.expected_new_name required for replace_with_new_component"
            )

    swap_strategy = row["swap_strategy"]
    parent_handling = row["parent_handling"]
    if swap_strategy not in SWAP_STRATEGIES:
        errors.append(f"{prefix}.swap_strategy invalid: {swap_strategy!r}")
    if parent_handling not in PARENT_HANDLING:
        errors.append(f"{prefix}.parent_handling invalid: {parent_handling!r}")

    validation = row["validation"]
    if not isinstance(validation, dict):
        errors.append(f"{prefix}.validation must be object")
        validation = {}
    if swap_strategy == "swap-direct" and validation.get("assert_variant_axes") is not True:
        errors.append(f"{prefix}: swap-direct requires variant-axis validation")
    if parent_handling == "compose-from-primitives" and status != "compose_from_primitives":
        errors.append(
            f"{prefix}: compose-from-primitives parent_handling requires "
            "target.status=compose_from_primitives"
        )

    translation = row["property_translation"]
    if not isinstance(translation, dict):
        errors.append(f"{prefix}.property_translation must be object")
        translation = {}
    kind = translation.get("kind")
    if not isinstance(kind, str) or not kind:
        errors.append(f"{prefix}.property_translation.kind must be non-empty string")
    if swap_strategy in {"create-instance-and-translate", "swap-with-translation"} and kind in {
        "none",
        "noop",
    }:
        errors.append(
            f"{prefix}: translation strategy requires a concrete property_translation.kind"
        )

    for key in VALIDATION_BOOLS:
        if not isinstance(validation.get(key), bool):
            errors.append(f"{prefix}.validation.{key} must be boolean")

    return errors


def validate_rule_against_census(
    row: dict[str, Any],
    idx: int,
    census: dict[str, str],
    target_registry_state: str,
) -> list[AuditFinding]:
    """Validate component target identity against optional census data."""
    target = row.get("target")
    if not isinstance(target, dict) or target.get("status") != "replace_with_new_component":
        return []
    new_key = target.get("new_key")
    expected_name = target.get("expected_new_name")
    if not isinstance(new_key, str) or not new_key:
        return []
    if target_registry_state == "not_probed":
        return [
            AuditFinding(
                status="warning",
                message=(
                    f"rules[{idx}].target.new_key was not checked against census; "
                    "pass --census figma/<file>/_census.md to verify target identity"
                ),
            )
        ]
    actual_name = census.get(new_key)
    if actual_name is None:
        return [
            AuditFinding(
                status="error",
                message=f"rules[{idx}].target.new_key {new_key!r} not found in census registry",
            )
        ]
    if expected_name and actual_name != expected_name:
        return [
            AuditFinding(
                status="error",
                message=(
                    f"rules[{idx}].target.expected_new_name {expected_name!r} "
                    f"does not match census name {actual_name!r}"
                ),
            )
        ]
    return []


def load_census_component_sets(paths: list[Path]) -> dict[str, str]:
    """Read component set key/name pairs from figmaclaw _census.md files."""
    result: dict[str, str] = {}
    for path in paths:
        result.update(load_census_registry(path))
    return result
