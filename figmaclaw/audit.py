"""Read-only audit helpers for design-system migration checks.

These helpers intentionally operate on Figma REST node trees and explicit
operator-provided migration artifacts. They do not read or write figmaclaw page
markdown bodies.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from figmaclaw.commands.census import load_census_registry
from figmaclaw.component_map import (
    FLAT_RULE_DISCRIMINATOR_ERROR_PREFIX,
    FLAT_SWAP_STRATEGIES,
    ComponentSetTaxonomy,
    FlatDirectRule,
    FlatRule,
    NestedRule,
    NestedRuleTarget,
    ValidationError,
    VariantTaxonomyDocument,
    format_validation_error,
    parse_flat_rule,
)
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
    # Optional human-readable rule identifier (e.g. "Buttons Desktop") so
    # operators reading lint output don't have to cross-reference rules[i]
    # back to the source map. (#167 review-3 finding #8.)
    rule_label: str | None = None


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


# Source-of-truth for the rule schema lives in figmaclaw.component_map. The
# constants below are kept for any external callers that imported them, but
# are derived from the pydantic models so they cannot drift.
from typing import get_args as _get_args  # noqa: E402

TARGET_STATUSES = set(_get_args(NestedRuleTarget.model_fields["status"].annotation))
SWAP_STRATEGIES = set(_get_args(NestedRule.model_fields["swap_strategy"].annotation))
PARENT_HANDLING = set(_get_args(NestedRule.model_fields["parent_handling"].annotation))
# expected_type is `NestedExpectedType | None`; first arg holds the Literal,
# and we re-include None to match the historical contract — pre-pydantic, the
# constant was used to validate a target's expected_type and accepted None
# as a valid omission. (Issue #167 review finding #8 / Copilot L98.)
EXPECTED_TYPES: set[str | None] = set(
    _get_args(_get_args(NestedRuleTarget.model_fields["expected_type"].annotation)[0])
) | {None}
VALIDATION_BOOLS = {
    "assert_target_type",
    "assert_name_matches",
    "assert_property_keys",
    "assert_variant_axes",
}


def _rule_is_flat_shape(row: dict[str, Any]) -> bool:
    """Return True when *row* uses the v3-flat instance-swap schema.

    We check shape (not validity): ``swap_strategy`` taken from the v3-flat
    vocabulary, OR no ``target`` block + a top-level ``new_key`` /
    ``audit_required``. Validation belongs to the pydantic models in
    :mod:`figmaclaw.component_map`.
    """
    swap_strategy = row.get("swap_strategy")
    if isinstance(swap_strategy, str) and swap_strategy in FLAT_SWAP_STRATEGIES:
        return True
    return "target" not in row and ("new_key" in row or row.get("audit_required") is True)


def _rule_target_summary(rule: FlatRule | NestedRule) -> tuple[str, str | None, str | None]:
    """Return ``(intent, new_key, expected_new_name)`` for any parsed rule.

    ``intent`` is one of the nested target statuses so the census check can be
    shared between schemas. A ``recompose_local`` rule that nevertheless
    names a concrete ``new_key`` (the recomposition reuses an existing TapIn
    primitive as the foundation) is treated as a census-checkable replacement
    so a typo in that key still surfaces — the lint shouldn't punish authors
    who include extra detail. (Issue #167 review finding #6.)
    """
    if isinstance(rule, NestedRule):
        return (
            rule.target.status,
            rule.target.new_key,
            rule.target.expected_new_name,
        )
    if isinstance(rule, FlatDirectRule):
        return ("replace_with_new_component", rule.new_key, rule.new_component_set)
    if rule.swap_strategy == "recompose_local":
        # Even though the canonical "intent" is compose_from_primitives, a
        # supplied new_key should be census-validated.
        return (
            "replace_with_new_component" if rule.new_key else "compose_from_primitives",
            rule.new_key,
            rule.new_component_set,
        )
    # FlatAuditOnlyRule never carries a new_key.
    return ("designer_audit_required", None, None)


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
    variants_paths: list[Path] | None = None,
) -> PipelineLintReport:
    """Validate the component migration map and optional target census data.

    ``variants_paths`` is an optional list of variant-taxonomy JSON files
    derived from the live Figma file (typically via use_figma calls to
    ``importComponentSetByKeyAsync(...)``). When provided, every rule with a
    ``variant_mapping`` is verified against the published variant axes of
    its NEW (and OLD, when available) component_set. See
    :func:`load_variant_taxonomies` for the accepted shape.
    """
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

    variants = load_variant_taxonomies(variants_paths or [])

    for idx, row in enumerate(rules):
        if not isinstance(row, dict):
            findings.append(AuditFinding(status="error", message=f"rules[{idx}] must be object"))
            continue
        prefix = f"rules[{idx}]"
        # Best-effort label from the raw row — readable even when pydantic
        # parse fails. We prefer old_component_set, fall back to old_key.
        rule_label: str | None = None
        if isinstance(row, dict):
            label = row.get("old_component_set") or row.get("old_key")
            if isinstance(label, str) and label:
                rule_label = label
        parsed: FlatRule | NestedRule | None = None
        try:
            parsed = (
                parse_flat_rule(row) if _rule_is_flat_shape(row) else NestedRule.model_validate(row)
            )
        except ValidationError as exc:
            for error in format_validation_error(prefix, exc):
                findings.append(AuditFinding(status="error", message=error, rule_label=rule_label))
            continue
        except ValueError as exc:
            # parse_flat_rule re-raises discriminator failures with a known
            # sentinel prefix; only surface those as v3-flat schema errors.
            # Any other ValueError (e.g. unexpected pydantic coercion path)
            # bubbles up so we don't silently misroute it.
            text = str(exc)
            if not text.startswith(FLAT_RULE_DISCRIMINATOR_ERROR_PREFIX):
                raise
            cleaned = text[len(FLAT_RULE_DISCRIMINATOR_ERROR_PREFIX) :]
            findings.append(
                AuditFinding(status="error", message=f"{prefix}: {cleaned}", rule_label=rule_label)
            )
            continue
        # Now that the rule parsed, prefer its typed old_component_set field.
        if (
            isinstance(parsed, (NestedRule, FlatDirectRule))
            and parsed.old_component_set
            or hasattr(parsed, "old_component_set")
            and parsed.old_component_set
        ):
            rule_label = parsed.old_component_set
        per_rule = validate_rule_against_census(
            parsed, idx, census, target_registry_state
        ) + validate_rule_variant_mapping(parsed, idx, variants)
        for finding in per_rule:
            if finding.rule_label is None:
                finding.rule_label = rule_label
            findings.append(finding)

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


def _rule_path_prefix(rule: FlatRule | NestedRule, idx: int) -> tuple[str, str]:
    """Return the dotted-path prefixes for ``new_key`` and ``expected_new_name``.

    v3-flat rules carry these at the top level (``rules[i].new_key``); the
    nested v3 schema nests them inside ``target`` (``rules[i].target.new_key``).
    Lint messages should use the path that *actually exists* in the input
    document so authors aren't sent looking for a `target` block that isn't
    there. (Issue #167 review finding #3 / Copilot L555.)
    """
    base = f"rules[{idx}]"
    if isinstance(rule, NestedRule):
        return f"{base}.target.new_key", f"{base}.target.expected_new_name"
    return f"{base}.new_key", f"{base}.new_component_set"


def validate_rule_against_census(
    rule: FlatRule | NestedRule,
    idx: int,
    census: dict[str, str],
    target_registry_state: str,
) -> list[AuditFinding]:
    """Validate component target identity against optional census data."""
    intent, new_key, expected_name = _rule_target_summary(rule)
    if intent != "replace_with_new_component":
        return []
    if not new_key:
        return []
    new_key_path, expected_name_path = _rule_path_prefix(rule, idx)
    if target_registry_state == "not_probed":
        return [
            AuditFinding(
                status="warning",
                message=(
                    f"{new_key_path} was not checked against census; "
                    "pass --census figma/<file>/_census.md to verify target identity"
                ),
            )
        ]
    actual_name = census.get(new_key)
    if actual_name is None:
        return [
            AuditFinding(
                status="error",
                message=f"{new_key_path} {new_key!r} not found in census registry",
            )
        ]
    if expected_name and actual_name != expected_name:
        return [
            AuditFinding(
                status="error",
                message=(
                    f"{expected_name_path} {expected_name!r} "
                    f"does not match census name {actual_name!r}"
                ),
            )
        ]
    return []


# Variant-axis lint (issue #163) ---------------------------------------------


_VARIANT_VALUE_PAIR = re.compile(r"\s*([^=,]+?)\s*=\s*([^,]+?)\s*(?:,|$)")


def parse_variant_axis_assignment(
    spec: str | dict[str, str],
) -> dict[str, str]:
    """Parse a variant_mapping right-hand side into ``{axis: value}`` pairs.

    Accepts either:

    * the dict shape used by the resolved swap manifest, e.g.
      ``{"Type": "Logo", "Colored": "True"}``
    * the string shape used by component_migration_map authoring, e.g.
      ``"color=primary, style=filled"`` — whitespace-tolerant.

    A non-conforming string returns an empty dict; the caller can flag the
    row separately. We intentionally do not raise — variant-axis validation
    runs after the rule has already passed the structural pydantic check.
    """
    if isinstance(spec, dict):
        return {
            str(k): str(v) for k, v in spec.items() if isinstance(k, str) and isinstance(v, str)
        }
    if not isinstance(spec, str):
        return {}
    pairs: dict[str, str] = {}
    for match in _VARIANT_VALUE_PAIR.finditer(spec):
        axis = match.group(1).strip()
        value = match.group(2).strip()
        if axis and value:
            pairs[axis] = value
    return pairs


def load_variant_taxonomies(paths: list[Path]) -> dict[str, ComponentSetTaxonomy]:
    """Load + merge variant-taxonomy sidecar files keyed by component_set key.

    Later files override earlier ones for the same key — useful when an
    operator splits taxonomy by file (one sidecar per Figma source file).
    """
    merged: dict[str, ComponentSetTaxonomy] = {}
    for path in paths:
        try:
            payload = load_json_file(path)
            doc = VariantTaxonomyDocument.model_validate(payload)
        except (OSError, ValueError, ValidationError) as exc:
            raise ValueError(f"{path}: invalid variant-taxonomy file: {exc}") from exc
        merged.update(doc.component_sets)
    return merged


def _classify_variant_mapping_shape(
    mapping: dict[str, str | dict[str, str]],
    new_taxonomy: ComponentSetTaxonomy,
) -> str:
    """Return ``"fixed"`` or ``"branching"`` for a ``variant_mapping`` block.

    * **fixed** — every key matches a published NEW axis name and every value
      is a scalar string. The mapping describes one assignment that applies to
      every OLD instance regardless of its OLD variant. Example::

          {"Type": "Logo", "Colored": "True"}

    * **branching** — keys are OLD axis values and each value is a
      string/dict describing the NEW axis assignment for that OLD value::

          {"Primary": "color=primary, style=filled",
           "Secondary": "color=secondary, style=filled"}

    Disambiguation: a mapping is fixed when *all* keys appear in the NEW
    component_set's published axis names AND every value is a non-empty
    scalar string.
    """
    new_axes = new_taxonomy.axis_names()
    if not mapping or not new_axes:
        return "branching"
    if not all(key in new_axes for key in mapping):
        return "branching"
    # An all-string mapping (including empty strings) classifies as fixed
    # so the empty-value case routes to the explicit "axis value cannot be
    # empty" error in `_check_value` instead of chain-erroring on
    # `parse_variant_axis_assignment("")` returning {}. (#167 review-3 #7.)
    if not all(isinstance(value, str) for value in mapping.values()):
        return "branching"
    return "fixed"


def validate_rule_variant_mapping(
    rule: FlatRule | NestedRule,
    idx: int,
    variants: dict[str, ComponentSetTaxonomy],
) -> list[AuditFinding]:
    """Lint a rule's ``variant_mapping`` against the published axis taxonomy.

    Two recognised mapping shapes — see ``_classify_variant_mapping_shape``:

    * **fixed**     — ``{NEW_axis_name: NEW_axis_value}``
    * **branching** — ``{OLD_axis_value: NEW_axis_assignment}``

    Per-shape checks:

    1. *fixed*: every key is a published NEW axis name; every value is a
       published value of that axis.
    2. *branching*: every right-hand assignment parses to ``{axis: value}``;
       every axis name is a published NEW axis; every value is a published
       value of that axis.
    3. *both*: when the OLD taxonomy is also provided in the variants
       document (keyed by ``old_key``), the union of left-hand keys and the
       fixed-shape assignment must exhaustively cover at least one published
       OLD axis so the swap step never falls into a silent default. (Per
       F29 in the consumer-repo rules.)

    Only flat ``swap_strategy=direct`` rules carry a ``variant_mapping``;
    other shapes are skipped silently.
    """
    if not isinstance(rule, FlatDirectRule):
        return []
    if not rule.variant_mapping:
        return []

    findings: list[AuditFinding] = []
    prefix = f"rules[{idx}]"

    if not variants:
        findings.append(
            AuditFinding(
                status="warning",
                message=(
                    f"{prefix}.variant_mapping was not checked against the published "
                    "variant taxonomy; pass --variants <taxonomy.json> to verify."
                ),
            )
        )
        return findings

    new_taxonomy = variants.get(rule.new_key)
    if new_taxonomy is None:
        findings.append(
            AuditFinding(
                status="error",
                message=(
                    f"{prefix}.new_key {rule.new_key!r} has no entry in the variant "
                    "taxonomy file; cannot validate variant_mapping"
                ),
            )
        )
        return findings

    new_axes = new_taxonomy.axis_names()
    # If the taxonomy entry exists but has no published axes, we cannot
    # discriminate fixed vs branching — every value would be misclassified
    # as branching and chain-error trying to parse "Logo" as `axis=value`.
    # Emit one explicit incompleteness warning and skip per-mapping checks
    # so the operator knows the taxonomy needs another collection pass.
    # (Issue #167 review finding #4 / Copilot L674.)
    if not new_axes:
        findings.append(
            AuditFinding(
                status="warning",
                message=(
                    f"{prefix}: variant taxonomy entry for new_key "
                    f"{rule.new_key!r} has no published axes; re-collect the "
                    "taxonomy with `componentSet.children` for that key before "
                    "the variant_mapping can be lint-validated"
                ),
            )
        )
        return findings

    shape = _classify_variant_mapping_shape(rule.variant_mapping, new_taxonomy)

    def _check_value(axis_name: str, axis_value: str, *, key_label: str) -> AuditFinding | None:
        """Validate a single axis=value pair against the new taxonomy.

        Empty-string values get an explicit "axis value cannot be empty"
        error — without this branch the branching shape would fall into a
        misleading "could not be parsed as `axis=value`" message for a key
        that *was* a published axis. (#167 review-3 finding #7.)

        Axes flagged ``inner_instance=True`` are F23 slot-on-the-leaf
        INSTANCE_SWAP slots (e.g. text-input's `_input-add-on`); their
        values are component_set keys or inner-instance variant names that
        the published variant taxonomy cannot enumerate, so we skip the
        membership check for those axes.
        """
        if axis_value == "":
            return AuditFinding(
                status="error",
                message=(
                    f"{prefix}.variant_mapping[{key_label}] sets axis "
                    f"{axis_name!r} to an empty string; remove the entry or "
                    "supply a published variant value"
                ),
            )
        axis = new_taxonomy.axes.get(axis_name)
        if axis is not None and axis.inner_instance:
            # F23 slot-on-the-leaf — value is a component_set key / inner
            # variant name that the published taxonomy cannot enumerate.
            return None
        allowed = new_taxonomy.values_for(axis_name)
        if allowed and axis_value not in allowed:
            return AuditFinding(
                status="error",
                message=(
                    f"{prefix}.variant_mapping[{key_label}] uses unknown "
                    f"value {axis_value!r} for axis {axis_name!r} on new "
                    f"component_set {rule.new_key!r}; published values: "
                    f"{sorted(allowed)}"
                ),
            )
        return None

    if shape == "fixed":
        for axis_name, axis_value in rule.variant_mapping.items():
            assert isinstance(axis_value, str)
            finding = _check_value(axis_name, axis_value, key_label=repr(axis_name))
            if finding is not None:
                findings.append(finding)
    else:
        for old_axis_value, raw_assignment in rule.variant_mapping.items():
            assignment = parse_variant_axis_assignment(raw_assignment)
            if not assignment:
                findings.append(
                    AuditFinding(
                        status="error",
                        message=(
                            f"{prefix}.variant_mapping[{old_axis_value!r}] could not be "
                            f"parsed as an `axis=value, axis=value` string or "
                            f"{{axis: value}} object; got {raw_assignment!r}"
                        ),
                    )
                )
                continue
            for axis_name, axis_value in assignment.items():
                if axis_name not in new_axes:
                    findings.append(
                        AuditFinding(
                            status="error",
                            message=(
                                f"{prefix}.variant_mapping[{old_axis_value!r}] references "
                                f"unknown axis {axis_name!r} on new component_set "
                                f"{rule.new_key!r}; published axes: {sorted(new_axes)}"
                            ),
                        )
                    )
                    continue
                finding = _check_value(axis_name, axis_value, key_label=repr(old_axis_value))
                if finding is not None:
                    findings.append(finding)

    # Coverage check: OLD axis values that the lint can reach. A "fixed"
    # variant_mapping covers every OLD instance trivially (no branching), so
    # the only coverage gap is when the mapping is "branching" and the OLD
    # taxonomy is published.
    old_taxonomy = variants.get(rule.old_key) if rule.old_key else None
    if shape == "branching" and old_taxonomy is not None:
        mapped_left = set(rule.variant_mapping)
        # Find at least one OLD axis whose values are referenced — this is the
        # axis the operator branched on. Coverage is enforced for that axis.
        for axis_name in old_taxonomy.axis_names():
            published = old_taxonomy.values_for(axis_name)
            if not published:
                continue
            if not (published & mapped_left):
                continue  # operator branched on a different axis
            missing = sorted(value for value in published if value not in mapped_left)
            if missing:
                findings.append(
                    AuditFinding(
                        status="error",
                        message=(
                            f"{prefix}.variant_mapping is missing entries for OLD axis "
                            f"{axis_name!r} value(s): {missing}; every published OLD "
                            "axis value must map to a NEW axis assignment so the swap "
                            "step never falls into a silent default."
                        ),
                    )
                )

    return findings


def load_census_component_sets(paths: list[Path]) -> dict[str, str]:
    """Read component set key/name pairs from figmaclaw _census.md files."""
    result: dict[str, str] = {}
    for path in paths:
        result.update(load_census_registry(path))
    return result
