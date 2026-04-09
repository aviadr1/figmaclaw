"""Scan a Figma page node tree for raw/stale/valid design token bindings.

Piggybacked on get_page() in pull_logic.py — the full recursive CANVAS tree
(including boundVariables on every node) is already in memory after get_page().
Zero additional API calls needed.

Token states:
  valid  — property bound to DS library (hash 778120a439be1fc5e95e31d08a39a2e70bed3e63)
  stale  — property bound to OLD_Gigaverse library (prefix a3972cba)
  raw    — no boundVariables binding at all (hardcoded value)

Detection rules (per-property):
  fills[i]:         boundVariables.fills[i].id — skip if fillStyleId set (covered by style)
  strokes[i]:       boundVariables.strokes[i].id
  strokeWeight:     boundVariables.strokeWeight.id (when node has strokes)
  cornerRadius:     boundVariables.cornerRadius.id (when non-zero)
  itemSpacing,
  paddingLeft/Right/Top/Bottom: boundVariables.<prop>.id (auto-layout nodes, non-zero)
  fontSize,
  fontFamily,
  fontWeight:       boundVariables.<prop>.id (TEXT nodes; skip all three if textStyleId set)

INSTANCE children are not recursed — overrides on the instance itself are checked,
but its internal children belong to the component definition, not the screen.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DS_LIB_HASH = "778120a439be1fc5e95e31d08a39a2e70bed3e63"
OLD_LIB_PREFIX = "a3972cba"

Classification = Literal["valid", "stale", "raw"]


class ValidBinding(BaseModel):
    """A resolved valid DS variable binding observed during a scan."""

    variable_id: str
    property: str
    hex: str | None = None
    numeric_value: float | None = None


_SPACING_PROPS = (
    "itemSpacing",
    "paddingLeft",
    "paddingRight",
    "paddingTop",
    "paddingBottom",
)
_FONT_PROPS = ("fontSize", "fontFamily", "fontWeight")


def classify_variable_id(var_id: str | None) -> Classification:
    """Classify a Figma variable ID as valid, stale, or raw.

    Variable IDs have the format ``VariableID:<lib_hash>/<var_id>``.
    The lib_hash prefix identifies the source library file.
    An absent or empty ID means the property is hardcoded (raw).
    An unknown library hash is treated as valid to avoid false positives.
    """
    if not var_id:
        return "raw"
    inner = var_id.removeprefix("VariableID:")
    lib_hash = inner.split("/")[0]
    if lib_hash == DS_LIB_HASH:
        return "valid"
    if lib_hash.startswith(OLD_LIB_PREFIX):
        return "stale"
    return "valid"  # unknown library — conservative, no false positive


def _get_bv_id(bv_entry: Any) -> str:
    """Extract the variable ID string from a boundVariables scalar entry."""
    if isinstance(bv_entry, dict):
        return bv_entry.get("id") or ""
    return ""


def _rgb_to_hex(color: dict[str, float]) -> str:
    r = round(color.get("r", 0) * 255)
    g = round(color.get("g", 0) * 255)
    b = round(color.get("b", 0) * 255)
    return f"#{r:02X}{g:02X}{b:02X}"


class TokenIssue(BaseModel):
    """A single raw or stale token binding on a specific node property."""

    node_id: str
    node_name: str
    node_type: str
    node_path: list[str]
    property: str
    index: int | None = None  # fill/stroke array slot; None for scalar properties
    classification: Classification
    current_value: Any = None  # color dict for fills/strokes; scalar for dimensions
    hex: str | None = None  # derived from current_value for color properties only
    stale_variable_id: str | None = None  # existing wrong binding ID (stale only)
    fix_variable_id: str | None = None  # null when written by pull; filled by suggest-tokens


class FrameTokenScan(BaseModel):
    """Token scan results for a single frame."""

    name: str
    raw: int = 0
    stale: int = 0
    valid: int = 0
    issues: list[TokenIssue] = Field(default_factory=list)
    valid_bindings: list[ValidBinding] = Field(default_factory=list)


class PageTokenScan(BaseModel):
    """Token scan results for a full page (totals + per-frame breakdown)."""

    raw: int = 0
    stale: int = 0
    valid: int = 0
    frames: dict[str, FrameTokenScan] = Field(default_factory=dict)
    valid_bindings: list[ValidBinding] = Field(default_factory=list)


def _scan_node(
    node: dict[str, Any],
    issues: list[TokenIssue],
    counters: dict[str, int],
    path: list[str],
    depth: int,
    valid_bindings: list[ValidBinding] | None = None,
) -> None:
    """Recursively walk a node, collecting token classification results.

    counters is mutated in-place: keys "raw", "stale", "valid".
    valid_bindings is mutated in-place when provided: collects resolved valid bindings.
    INSTANCE children are not recursed (their internals belong to the component).
    """
    if depth > 8:
        return

    node_id = node.get("id", "")
    node_name = node.get("name", "")
    node_type = node.get("type", "")
    node_path = [*path, node_name]
    bv: dict[str, Any] = node.get("boundVariables") or {}
    is_text = node_type == "TEXT"

    def record(
        prop: str,
        cls: Classification,
        current_value: Any,
        idx: int | None = None,
        stale_var_id: str | None = None,
        var_id: str | None = None,
    ) -> None:
        counters[cls] = counters.get(cls, 0) + 1
        if cls == "valid":
            if valid_bindings is not None and var_id:
                binding = ValidBinding(variable_id=var_id, property=prop)
                if isinstance(current_value, dict) and "r" in current_value:
                    binding.hex = _rgb_to_hex(current_value)
                elif isinstance(current_value, int | float):
                    binding.numeric_value = float(current_value)
                valid_bindings.append(binding)
            return
        issue = TokenIssue(
            node_id=node_id,
            node_name=node_name,
            node_type=node_type,
            node_path=node_path,
            property=prop,
            index=idx,
            classification=cls,
            current_value=current_value,
        )
        if isinstance(current_value, dict) and "r" in current_value:
            issue.hex = _rgb_to_hex(current_value)
        if stale_var_id:
            issue.stale_variable_id = stale_var_id
        issues.append(issue)

    # fills
    fills: list[dict] = node.get("fills") or []
    fills_bv: list[Any] = bv.get("fills") or []
    has_fill_style = bool(node.get("fillStyleId"))
    for i, fill in enumerate(fills):
        if fill.get("type") != "SOLID" or fill.get("visible") is False:
            continue
        if has_fill_style:
            counters["valid"] = counters.get("valid", 0) + 1
            continue
        cbv = fills_bv[i] if isinstance(fills_bv, list) and i < len(fills_bv) else None
        var_id = _get_bv_id(cbv)
        cls = classify_variable_id(var_id)
        record(
            "fill",
            cls,
            fill.get("color"),
            idx=i,
            stale_var_id=var_id if cls == "stale" else None,
            var_id=var_id if cls == "valid" else None,
        )

    # strokes
    strokes: list[dict] = node.get("strokes") or []
    strokes_bv: list[Any] = bv.get("strokes") or []
    for i, stroke in enumerate(strokes):
        if stroke.get("type") != "SOLID" or stroke.get("visible") is False:
            continue
        cbv = strokes_bv[i] if isinstance(strokes_bv, list) and i < len(strokes_bv) else None
        var_id = _get_bv_id(cbv)
        cls = classify_variable_id(var_id)
        record(
            "stroke",
            cls,
            stroke.get("color"),
            idx=i,
            stale_var_id=var_id if cls == "stale" else None,
            var_id=var_id if cls == "valid" else None,
        )

    # strokeWeight (only when node has at least one stroke)
    if strokes and node.get("strokeWeight") is not None:
        var_id = _get_bv_id(bv.get("strokeWeight"))
        cls = classify_variable_id(var_id)
        record(
            "strokeWeight",
            cls,
            node.get("strokeWeight"),
            stale_var_id=var_id if cls == "stale" else None,
            var_id=var_id if cls == "valid" else None,
        )

    # cornerRadius (non-zero scalars only; "mixed" means individual corners are set)
    cr = node.get("cornerRadius")
    if cr is not None and cr != "mixed" and cr != 0:
        var_id = _get_bv_id(bv.get("cornerRadius"))
        cls = classify_variable_id(var_id)
        record(
            "cornerRadius",
            cls,
            cr,
            stale_var_id=var_id if cls == "stale" else None,
            var_id=var_id if cls == "valid" else None,
        )

    # gap and padding (auto-layout nodes)
    if node.get("layoutMode") and node.get("layoutMode") != "NONE":
        for prop in _SPACING_PROPS:
            val = node.get(prop)
            if val is not None and val != 0:
                var_id = _get_bv_id(bv.get(prop))
                cls = classify_variable_id(var_id)
                record(
                    prop,
                    cls,
                    val,
                    stale_var_id=var_id if cls == "stale" else None,
                    var_id=var_id if cls == "valid" else None,
                )

    # font properties (TEXT nodes only)
    if is_text:
        has_text_style = bool(node.get("textStyleId") or (node.get("styles") or {}).get("text"))
        if has_text_style:
            # All three font props are covered by the text style → valid
            counters["valid"] = counters.get("valid", 0) + len(_FONT_PROPS)
        else:
            for prop in _FONT_PROPS:
                val = node.get(prop)
                if val is not None:
                    var_id = _get_bv_id(bv.get(prop))
                    cls = classify_variable_id(var_id)
                    record(
                        prop,
                        cls,
                        val,
                        stale_var_id=var_id if cls == "stale" else None,
                        var_id=var_id if cls == "valid" else None,
                    )

    # recurse — skip INSTANCE children (component internals, not this screen's concern)
    if node_type == "INSTANCE":
        return
    for child in node.get("children") or []:
        _scan_node(child, issues, counters, node_path, depth + 1, valid_bindings)


def scan_frame(frame_node: dict[str, Any]) -> FrameTokenScan:
    """Scan a single FRAME node (including its own properties and all descendants)."""
    issues: list[TokenIssue] = []
    counters: dict[str, int] = {}
    valid_bindings: list[ValidBinding] = []
    _scan_node(frame_node, issues, counters, path=[], depth=0, valid_bindings=valid_bindings)
    return FrameTokenScan(
        name=frame_node.get("name", ""),
        raw=counters.get("raw", 0),
        stale=counters.get("stale", 0),
        valid=counters.get("valid", 0),
        issues=issues,
        valid_bindings=valid_bindings,
    )


def scan_page(page_node: dict[str, Any], frame_ids: set[str]) -> PageTokenScan:
    """Scan all frames in frame_ids found within page_node.

    Walks the CANVAS tree to find each target FRAME, calls scan_frame on it,
    and aggregates results. Only frame IDs listed in frame_ids are scanned —
    component library frames are skipped.

    Returns a PageTokenScan with page-level totals and per-frame detail.
    """
    result = PageTokenScan()

    def _find_and_scan(node: dict[str, Any]) -> None:
        node_id = node.get("id", "")
        node_type = node.get("type", "")
        if node_type in ("FRAME", "COMPONENT") and node_id in frame_ids:
            fscan = scan_frame(node)
            result.raw += fscan.raw
            result.stale += fscan.stale
            result.valid += fscan.valid
            result.valid_bindings.extend(fscan.valid_bindings)
            # Sparse — only include frames that have at least one actionable issue
            if fscan.raw > 0 or fscan.stale > 0:
                result.frames[node_id] = fscan
            return  # scan_frame already walked this subtree; don't recurse again
        for child in node.get("children") or []:
            _find_and_scan(child)

    for child in page_node.get("children") or []:
        _find_and_scan(child)

    return result
