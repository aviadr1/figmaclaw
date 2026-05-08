"""Pydantic models for component-migration maps and variant taxonomies.

The ``component_migration_map.v3.json`` artifact is the contract between the
human/Claude authoring stage of a design-system migration and the figmaclaw
swap / lint pipeline. It comes in two compatible shapes:

* **Nested v3** — the original, validated by :class:`NestedRule`. Has a
  ``target`` block carrying ``status`` / ``new_key`` / ``expected_type`` and
  a ``swap_strategy`` taken from the verb set
  ``{create-instance-and-translate, swap-with-translation, swap-direct, none}``.

* **Flat v3** — introduced for instance-swap migrations driven by
  ``audit-page swap``. Validated by the discriminated union
  :class:`FlatDirectRule` / :class:`FlatRecomposeLocalRule` /
  :class:`FlatAuditOnlyRule` on the ``swap_strategy`` literal
  ``{direct, recompose_local, audit_only}``. Top-level fields carry the swap
  intent directly: ``new_key``, ``variant_mapping``, ``preserve``,
  ``recomposition_plan``.

The :class:`VariantTaxonomyDocument` sidecar (typically produced by a
use_figma call that fetches ``importComponentSetByKeyAsync`` for each TapIn
target) lets the lint validate that every ``variant_mapping`` references real
published axes and values on the target component_set, and that the OLD
component_set's axis values are exhaustively covered.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

# Vocabulary -----------------------------------------------------------------

NestedTargetStatus = Literal[
    "replace_with_new_component",
    "compose_from_primitives",
    "designer_audit_required",
    "discard_on_parent_swap",
]
NestedSwapStrategy = Literal[
    "create-instance-and-translate",
    "swap-with-translation",
    "swap-direct",
    "none",
]
NestedParentHandling = Literal[
    "leave-as-instance",
    "detach-then-swap-inners",
    "compose-from-primitives",
]
NestedExpectedType = Literal["COMPONENT_SET", "COMPONENT", "FRAME"]

FLAT_SWAP_STRATEGIES: tuple[str, ...] = ("direct", "recompose_local", "audit_only")
FLAT_AUDIT_KINDS: tuple[str, ...] = (
    "recomposition_proposal",
    "missing_tapin_primitive",
    "needs_variant_validation",
    "manual_review",
)
FLAT_CONFIDENCES: tuple[str, ...] = (
    "high",
    "medium",
    "low",
    "needs_variant_validation",
)

NESTED_TARGET_STATUSES: tuple[str, ...] = (
    "replace_with_new_component",
    "compose_from_primitives",
    "designer_audit_required",
    "discard_on_parent_swap",
)
NESTED_SWAP_STRATEGIES: tuple[str, ...] = (
    "create-instance-and-translate",
    "swap-with-translation",
    "swap-direct",
    "none",
)
NESTED_PARENT_HANDLING: tuple[str, ...] = (
    "leave-as-instance",
    "detach-then-swap-inners",
    "compose-from-primitives",
)
NESTED_EXPECTED_TYPES: tuple[str, ...] = ("COMPONENT_SET", "COMPONENT", "FRAME")
NESTED_VALIDATION_BOOLS: tuple[str, ...] = (
    "assert_target_type",
    "assert_name_matches",
    "assert_property_keys",
    "assert_variant_axes",
)

FlatConfidence = Literal["high", "medium", "low", "needs_variant_validation"]
FlatAuditKind = Literal[
    "recomposition_proposal",
    "missing_tapin_primitive",
    "needs_variant_validation",
    "manual_review",
]


class _Loose(BaseModel):
    """Base with a config that tolerates unknown sibling keys.

    The component-migration map evolves field-by-field as migration practice
    discovers new things to track (e.g. ``confidence``, ``notes``,
    ``old_componentId_examples``). Forbidding unknown keys would force every
    new field to ship a schema bump; we keep the schema permissive so that
    additive changes propagate without churn, and rely on explicit
    declarations on the fields the lint actually consumes.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=False)


# Nested v3 rule -------------------------------------------------------------


class NestedRuleTarget(_Loose):
    status: NestedTargetStatus
    new_key: str | None = None
    expected_type: NestedExpectedType | None = None
    expected_new_name: str | None = None

    @model_validator(mode="after")
    def _replace_requires_new_key(self) -> NestedRuleTarget:
        if self.status == "replace_with_new_component":
            if not self.new_key:
                raise ValueError("new_key required for status=replace_with_new_component")
            if self.expected_type != "COMPONENT_SET":
                raise ValueError(
                    "expected_type must be COMPONENT_SET for status=replace_with_new_component"
                )
            if not self.expected_new_name:
                raise ValueError("expected_new_name required for status=replace_with_new_component")
        return self


class NestedRuleValidation(_Loose):
    assert_target_type: bool
    assert_name_matches: bool
    assert_property_keys: bool
    assert_variant_axes: bool


class NestedRulePropertyTranslation(_Loose):
    kind: str = Field(min_length=1)


class NestedRule(_Loose):
    """Rule using the original v3 nested schema (target block + 4 sibling keys)."""

    old_component_set: str = Field(min_length=1)
    old_key: str = Field(min_length=1)
    target: NestedRuleTarget
    swap_strategy: NestedSwapStrategy
    parent_handling: NestedParentHandling
    property_translation: NestedRulePropertyTranslation
    validation: NestedRuleValidation

    @model_validator(mode="after")
    def _coupling_rules(self) -> NestedRule:
        if self.swap_strategy == "swap-direct" and not self.validation.assert_variant_axes:
            raise ValueError("swap-direct requires validation.assert_variant_axes=true")
        if (
            self.parent_handling == "compose-from-primitives"
            and self.target.status != "compose_from_primitives"
        ):
            raise ValueError(
                "parent_handling=compose-from-primitives requires "
                "target.status=compose_from_primitives"
            )
        if self.swap_strategy in {
            "create-instance-and-translate",
            "swap-with-translation",
        } and self.property_translation.kind in {"none", "noop"}:
            raise ValueError("translation strategies require a concrete property_translation.kind")
        return self


# Flat v3 rule ---------------------------------------------------------------


class RecompositionPlan(_Loose):
    new_local_name: str = Field(min_length=1)
    structure: Any  # Free-form for now; the recompose pipeline interprets it.
    tapin_primitives: list[str] | None = None
    color_bindings: dict[str, str] | None = None


class _FlatRuleBase(_Loose):
    old_component_set: str | None = None
    old_key: str = Field(min_length=1)
    confidence: FlatConfidence | None = None
    notes: str | None = None
    preserve: list[str] | None = None
    old_componentId_examples: list[str] | None = None


class FlatDirectRule(_FlatRuleBase):
    """``swap_strategy=direct`` rules — drive an instance-swap script row."""

    swap_strategy: Literal["direct"]
    new_component_set: str | None = None
    new_key: str = Field(min_length=1)
    variant_mapping: dict[str, str | dict[str, str]] = Field(default_factory=dict)

    @field_validator("variant_mapping")
    @classmethod
    def _variant_mapping_keys_non_empty(
        cls, value: dict[str, str | dict[str, str]]
    ) -> dict[str, str | dict[str, str]]:
        for key in value:
            if not isinstance(key, str) or not key:
                raise ValueError("variant_mapping keys must be non-empty strings")
        return value


class FlatRecomposeLocalRule(_FlatRuleBase):
    """Rules that require building a new local component before swapping."""

    swap_strategy: Literal["recompose_local"]
    new_component_set: str | None = None
    new_key: str | None = None
    recomposition_plan: RecompositionPlan


class FlatAuditOnlyRule(_FlatRuleBase):
    """Rules that surface in the designer-audit list and never auto-swap."""

    swap_strategy: Literal["audit_only"]
    audit_required: Literal[True]
    audit_kind: FlatAuditKind


FlatRule = Annotated[
    FlatDirectRule | FlatRecomposeLocalRule | FlatAuditOnlyRule,
    Field(discriminator="swap_strategy"),
]

FlatRuleAdapter: TypeAdapter[FlatRule] = TypeAdapter(FlatRule)


def parse_flat_rule(payload: dict[str, Any]) -> FlatRule:
    """Parse a single flat-shape rule.

    Re-raises pydantic's discriminator-extraction error with an
    author-friendly message that lists the accepted ``swap_strategy`` values
    — by default the user sees ``Unable to extract tag using discriminator
    'swap_strategy'``, which is opaque if you don't know it's a discriminated
    union under the hood. (Issue #167 review finding, parse_flat_rule.)
    """
    try:
        return FlatRuleAdapter.validate_python(payload)
    except ValidationError as exc:
        # Walk the errors to find a discriminator failure; if there is one,
        # raise a fresh ValidationError-like error with FLAT_SWAP_STRATEGIES.
        for err in exc.errors(include_url=False):
            if err.get("type") in {"union_tag_invalid", "union_tag_not_found"}:
                got = payload.get("swap_strategy")
                raise ValueError(
                    f"v3-flat rule swap_strategy must be one of "
                    f"{sorted(FLAT_SWAP_STRATEGIES)}; got {got!r}. "
                    "If this rule is meant to use the v3-nested schema, add a "
                    "`target` block (then it will be validated as nested)."
                ) from exc
        raise


# Variant taxonomy sidecar ---------------------------------------------------


class VariantAxis(_Loose):
    """One published variant axis on a component_set.

    For top-level axes, ``values`` is the set of distinct option strings the
    publisher exposes. For an inner-instance INSTANCE_SWAP axis (the
    "slot-on-the-leaf" pattern, F23), ``values`` is the set of accepted
    component-set keys or the published variant names of the slot's
    component-set.
    """

    values: list[str] = Field(default_factory=list)
    inner_instance: bool = False


class ComponentSetTaxonomy(_Loose):
    """Variant taxonomy for one component_set, keyed by axis name."""

    name: str | None = None
    file_key: str | None = None
    axes: dict[str, VariantAxis] = Field(default_factory=dict)

    def axis_names(self) -> set[str]:
        return set(self.axes)

    def values_for(self, axis_name: str) -> set[str]:
        axis = self.axes.get(axis_name)
        return set(axis.values) if axis else set()


class VariantTaxonomyDocument(_Loose):
    """Sidecar file mapping component_set keys → :class:`ComponentSetTaxonomy`.

    Accepted on disk as either::

        {"component_sets": {"<key>": {<taxonomy>}}}

    or the equivalent flat form::

        {"<key>": {<taxonomy>}}

    The latter is what falls naturally out of a use_figma call that loops
    over rule new_keys.
    """

    component_sets: dict[str, ComponentSetTaxonomy] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_form(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "component_sets" in value:
            return value
        return {"component_sets": value}


# Helpers for validation-error formatting ------------------------------------


def format_validation_error(prefix: str, error: ValidationError) -> list[str]:
    """Format a pydantic ValidationError as a list of lint-friendly strings."""
    out: list[str] = []
    for err in error.errors(include_url=False):
        loc = ".".join(str(part) for part in err.get("loc") or ())
        msg = err.get("msg") or "invalid"
        if loc:
            out.append(f"{prefix}.{loc}: {msg}")
        else:
            out.append(f"{prefix}: {msg}")
    return out


__all__ = [
    "ComponentSetTaxonomy",
    "FLAT_AUDIT_KINDS",
    "FLAT_CONFIDENCES",
    "FLAT_SWAP_STRATEGIES",
    "FlatAuditOnlyRule",
    "FlatDirectRule",
    "FlatRecomposeLocalRule",
    "FlatRule",
    "FlatRuleAdapter",
    "NESTED_EXPECTED_TYPES",
    "NESTED_PARENT_HANDLING",
    "NESTED_SWAP_STRATEGIES",
    "NESTED_TARGET_STATUSES",
    "NESTED_VALIDATION_BOOLS",
    "NestedRule",
    "NestedRulePropertyTranslation",
    "NestedRuleTarget",
    "NestedRuleValidation",
    "RecompositionPlan",
    "ValidationError",
    "VariantAxis",
    "VariantTaxonomyDocument",
    "format_validation_error",
    "parse_flat_rule",
]
