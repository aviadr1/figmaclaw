"""Tests for raw/stale/valid design token scanning.

INVARIANTS:
- classify_variable_id correctly identifies DS lib hash as valid
- classify_variable_id correctly identifies OLD lib prefix as stale
- classify_variable_id returns raw for absent/empty IDs
- classify_variable_id treats unknown library hashes as valid (no false positives)
- scan_frame counts raw fills on nodes without boundVariables
- scan_frame counts stale fills on nodes bound to OLD library
- scan_frame does not count fills covered by fillStyleId
- scan_frame does not count font properties when textStyleId is set
- scan_frame does not recurse into INSTANCE children
- scan_frame includes the frame's own properties
- scan_page returns only frames with at least one issue in frames dict
- scan_page totals equal sum of per-frame totals
- TokenIssue hex is set only for color properties
- stale issues include stale_variable_id
"""
from __future__ import annotations

from figmaclaw.token_scan import (
    DS_LIB_HASH,
    OLD_LIB_PREFIX,
    FrameTokenScan,
    TokenIssue,
    classify_variable_id,
    scan_frame,
    scan_page,
)

DS_VAR_ID = f"VariableID:{DS_LIB_HASH}/1234:5"
STALE_VAR_ID = f"VariableID:{OLD_LIB_PREFIX}abc123/1234:5"
OTHER_VAR_ID = "VariableID:deadbeef1234567890abcdef12345678/9999:1"

RED_COLOR = {"r": 1.0, "g": 0.0, "b": 0.0, "a": 1.0}
DARK_COLOR = {"r": 0.08, "g": 0.08, "b": 0.12, "a": 1.0}


def _solid_fill(color: dict | None = None, *, var_id: str | None = None) -> tuple[dict, dict | None]:
    """Return (fill_dict, fills_bv_entry) for a SOLID fill."""
    fill = {"type": "SOLID", "color": color or RED_COLOR}
    bv_entry = {"id": var_id} if var_id else None
    return fill, bv_entry


def _frame(
    node_id: str = "1:1",
    name: str = "Frame",
    children: list[dict] | None = None,
    fills: list[dict] | None = None,
    fills_bv: list | None = None,
    fill_style_id: str | None = None,
    corner_radius: float | None = None,
    corner_radius_bv: str | None = None,
) -> dict:
    node: dict = {"id": node_id, "name": name, "type": "FRAME", "children": children or []}
    if fills is not None:
        node["fills"] = fills
    if fills_bv is not None:
        node.setdefault("boundVariables", {})["fills"] = fills_bv
    if fill_style_id:
        node["fillStyleId"] = fill_style_id
    if corner_radius is not None:
        node["cornerRadius"] = corner_radius
    if corner_radius_bv is not None:
        node.setdefault("boundVariables", {})["cornerRadius"] = {"id": corner_radius_bv}
    return node


def _rect(
    node_id: str = "2:1",
    name: str = "Rect",
    fills: list[dict] | None = None,
    fills_bv: list | None = None,
    fill_style_id: str | None = None,
    strokes: list[dict] | None = None,
    strokes_bv: list | None = None,
    stroke_weight: float | None = None,
    stroke_weight_bv: str | None = None,
    corner_radius: float | None = None,
    corner_radius_bv: str | None = None,
) -> dict:
    node: dict = {"id": node_id, "name": name, "type": "RECTANGLE"}
    if fills is not None:
        node["fills"] = fills
    if fills_bv is not None:
        node.setdefault("boundVariables", {})["fills"] = fills_bv
    if fill_style_id:
        node["fillStyleId"] = fill_style_id
    if strokes is not None:
        node["strokes"] = strokes
    if strokes_bv is not None:
        node.setdefault("boundVariables", {})["strokes"] = strokes_bv
    if stroke_weight is not None:
        node["strokeWeight"] = stroke_weight
    if stroke_weight_bv is not None:
        node.setdefault("boundVariables", {})["strokeWeight"] = {"id": stroke_weight_bv}
    if corner_radius is not None:
        node["cornerRadius"] = corner_radius
    if corner_radius_bv is not None:
        node.setdefault("boundVariables", {})["cornerRadius"] = {"id": corner_radius_bv}
    return node


def _text(
    node_id: str = "3:1",
    name: str = "Text",
    fills: list[dict] | None = None,
    fills_bv: list | None = None,
    font_size: float | None = None,
    font_size_bv: str | None = None,
    font_family: str | None = None,
    font_family_bv: str | None = None,
    font_weight: float | None = None,
    font_weight_bv: str | None = None,
    text_style_id: str | None = None,
) -> dict:
    node: dict = {"id": node_id, "name": name, "type": "TEXT"}
    if fills is not None:
        node["fills"] = fills
    if fills_bv is not None:
        node.setdefault("boundVariables", {})["fills"] = fills_bv
    if font_size is not None:
        node["fontSize"] = font_size
    if font_size_bv is not None:
        node.setdefault("boundVariables", {})["fontSize"] = {"id": font_size_bv}
    if font_family is not None:
        node["fontFamily"] = font_family
    if font_family_bv is not None:
        node.setdefault("boundVariables", {})["fontFamily"] = {"id": font_family_bv}
    if font_weight is not None:
        node["fontWeight"] = font_weight
    if font_weight_bv is not None:
        node.setdefault("boundVariables", {})["fontWeight"] = {"id": font_weight_bv}
    if text_style_id:
        node["textStyleId"] = text_style_id
    return node


def _instance(node_id: str = "9:1", name: str = "Button", children: list[dict] | None = None) -> dict:
    return {"id": node_id, "name": name, "type": "INSTANCE", "children": children or []}


def _page(children: list[dict]) -> dict:
    return {"id": "0:1", "name": "Page", "type": "CANVAS", "children": children}


# --- classify_variable_id ---

def test_classify_ds_lib_hash_is_valid():
    assert classify_variable_id(DS_VAR_ID) == "valid"


def test_classify_old_lib_prefix_is_stale():
    assert classify_variable_id(STALE_VAR_ID) == "stale"


def test_classify_none_is_raw():
    assert classify_variable_id(None) == "raw"


def test_classify_empty_string_is_raw():
    assert classify_variable_id("") == "raw"


def test_classify_unknown_lib_is_valid():
    """Unknown library hash → valid (conservative, no false positives)."""
    assert classify_variable_id(OTHER_VAR_ID) == "valid"


# --- scan_frame: fills ---

def test_scan_frame_raw_fill_is_counted():
    """INVARIANT: a fill with no variable binding is counted as raw."""
    fill, _ = _solid_fill(RED_COLOR)
    frame = _frame(children=[_rect(fills=[fill])])
    result = scan_frame(frame)
    assert result.raw == 1
    assert result.stale == 0
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.property == "fill"
    assert issue.index == 0
    assert issue.classification == "raw"
    assert issue.hex == "#FF0000"


def test_scan_frame_valid_fill_not_in_issues():
    """INVARIANT: a DS-bound fill produces no issue and is counted as valid."""
    fill, bv_entry = _solid_fill(RED_COLOR, var_id=DS_VAR_ID)
    frame = _frame(children=[_rect(fills=[fill], fills_bv=[bv_entry])])
    result = scan_frame(frame)
    assert result.raw == 0
    assert result.valid == 1
    assert result.issues == []


def test_scan_frame_stale_fill_is_counted():
    """INVARIANT: a fill bound to the OLD library is counted as stale."""
    fill, bv_entry = _solid_fill(DARK_COLOR, var_id=STALE_VAR_ID)
    frame = _frame(children=[_rect(fills=[fill], fills_bv=[bv_entry])])
    result = scan_frame(frame)
    assert result.stale == 1
    assert result.raw == 0
    assert result.issues[0].classification == "stale"
    assert result.issues[0].stale_variable_id == STALE_VAR_ID


def test_scan_frame_fill_style_id_is_valid():
    """INVARIANT: a fill covered by fillStyleId is treated as valid (no issue)."""
    fill, _ = _solid_fill(RED_COLOR)
    frame = _frame(children=[
        _rect(fills=[fill], fill_style_id="S:abc123")
    ])
    result = scan_frame(frame)
    assert result.raw == 0
    assert result.valid == 1
    assert result.issues == []


def test_scan_frame_invisible_fill_skipped():
    """INVARIANT: invisible fills (visible=False) are not classified."""
    fill = {"type": "SOLID", "color": RED_COLOR, "visible": False}
    frame = _frame(children=[_rect(fills=[fill])])
    result = scan_frame(frame)
    assert result.raw == 0
    assert result.issues == []


def test_scan_frame_non_solid_fill_skipped():
    """INVARIANT: non-SOLID fills (gradients, images) are not classified."""
    fill = {"type": "GRADIENT_LINEAR", "gradientStops": []}
    frame = _frame(children=[_rect(fills=[fill])])
    result = scan_frame(frame)
    assert result.raw == 0
    assert result.issues == []


# --- scan_frame: frame's own properties ---

def test_scan_frame_own_fill_is_checked():
    """INVARIANT: the frame node's own fills are included in the scan."""
    fill, _ = _solid_fill(RED_COLOR)
    frame = _frame(fills=[fill])
    result = scan_frame(frame)
    assert result.raw == 1


def test_scan_frame_corner_radius_raw():
    """INVARIANT: a non-zero cornerRadius without binding is counted as raw."""
    frame = _frame(children=[_rect(corner_radius=8.0)])
    result = scan_frame(frame)
    assert result.raw == 1
    assert result.issues[0].property == "cornerRadius"
    assert result.issues[0].index is None
    assert result.issues[0].current_value == 8.0


def test_scan_frame_corner_radius_valid():
    """INVARIANT: cornerRadius bound to DS lib is counted as valid."""
    frame = _frame(children=[_rect(corner_radius=8.0, corner_radius_bv=DS_VAR_ID)])
    result = scan_frame(frame)
    assert result.valid == 1
    assert result.raw == 0
    assert result.issues == []


def test_scan_frame_zero_corner_radius_skipped():
    """INVARIANT: cornerRadius == 0 is not counted (no meaningful constraint)."""
    frame = _frame(children=[_rect(corner_radius=0)])
    result = scan_frame(frame)
    assert result.raw == 0


# --- scan_frame: strokes ---

def test_scan_frame_raw_stroke_is_counted():
    """INVARIANT: a stroke with no variable binding is counted as raw."""
    stroke = {"type": "SOLID", "color": RED_COLOR}
    frame = _frame(children=[_rect(strokes=[stroke])])
    result = scan_frame(frame)
    assert result.raw >= 1
    stroke_issues = [i for i in result.issues if i.property == "stroke"]
    assert len(stroke_issues) == 1


def test_scan_frame_stroke_weight_raw():
    """INVARIANT: strokeWeight without binding is counted when strokes exist."""
    stroke = {"type": "SOLID", "color": RED_COLOR}
    frame = _frame(children=[_rect(strokes=[stroke], stroke_weight=2.0)])
    result = scan_frame(frame)
    props = {i.property for i in result.issues}
    assert "strokeWeight" in props


def test_scan_frame_stroke_weight_not_counted_without_strokes():
    """INVARIANT: strokeWeight is only checked when the node has strokes."""
    frame = _frame(children=[_rect(stroke_weight=2.0)])  # no strokes list
    result = scan_frame(frame)
    sw_issues = [i for i in result.issues if i.property == "strokeWeight"]
    assert sw_issues == []


# --- scan_frame: text properties ---

def test_scan_frame_raw_font_size_is_counted():
    """INVARIANT: fontSize without binding (and no textStyleId) is counted as raw."""
    text = _text(font_size=16.0)
    frame = _frame(children=[text])
    result = scan_frame(frame)
    assert result.raw >= 1
    font_issues = [i for i in result.issues if i.property == "fontSize"]
    assert len(font_issues) == 1


def test_scan_frame_text_style_id_covers_all_font_props():
    """INVARIANT: textStyleId means all three font props are valid — no issues."""
    text = _text(font_size=16.0, font_family="Figtree", font_weight=400.0,
                 text_style_id="S:text-body")
    frame = _frame(children=[text])
    result = scan_frame(frame)
    assert result.raw == 0
    assert result.valid == 3  # fontSize, fontFamily, fontWeight all valid via style
    assert result.issues == []


def test_scan_frame_valid_font_size_not_in_issues():
    """INVARIANT: DS-bound fontSize produces no issue."""
    text = _text(font_size=16.0, font_size_bv=DS_VAR_ID)
    frame = _frame(children=[text])
    result = scan_frame(frame)
    font_issues = [i for i in result.issues if i.property == "fontSize"]
    assert font_issues == []


# --- scan_frame: INSTANCE handling ---

def test_scan_frame_instance_own_fill_checked():
    """INVARIANT: fills directly on an INSTANCE node (overrides) are classified."""
    fill, _ = _solid_fill(RED_COLOR)
    inst = _instance(node_id="9:1")
    inst["fills"] = [fill]
    frame = _frame(children=[inst])
    result = scan_frame(frame)
    assert result.raw == 1


def test_scan_frame_instance_children_not_recursed():
    """INVARIANT: children of an INSTANCE are not recursed into (component internals)."""
    raw_fill, _ = _solid_fill(RED_COLOR)
    inner_rect = _rect(fills=[raw_fill])
    inst = _instance(node_id="9:1", children=[inner_rect])
    frame = _frame(children=[inst])
    result = scan_frame(frame)
    # The inner_rect fill belongs to the component — should not be counted
    assert result.raw == 0
    assert result.issues == []


# --- scan_frame: hex derivation ---

def test_scan_frame_hex_derived_for_color_issues():
    """INVARIANT: hex field is set on issues with color current_value."""
    fill, _ = _solid_fill({"r": 0.0, "g": 1.0, "b": 0.0, "a": 1.0})
    frame = _frame(children=[_rect(fills=[fill])])
    result = scan_frame(frame)
    assert result.issues[0].hex == "#00FF00"


def test_scan_frame_hex_not_set_for_scalar_issues():
    """INVARIANT: hex is None for non-color issues (cornerRadius, etc.)."""
    frame = _frame(children=[_rect(corner_radius=8.0)])
    result = scan_frame(frame)
    assert result.issues[0].hex is None


# --- scan_page ---

def test_scan_page_sparse_only_frames_with_issues():
    """INVARIANT: scan_page result.frames only contains frames with raw or stale issues."""
    fill, bv = _solid_fill(RED_COLOR, var_id=DS_VAR_ID)
    clean_frame = _frame(node_id="1:1", children=[_rect(fills=[fill], fills_bv=[bv])])
    raw_fill, _ = _solid_fill(RED_COLOR)
    dirty_frame = _frame(node_id="2:1", children=[_rect(fills=[raw_fill])])
    page = _page([clean_frame, dirty_frame])

    result = scan_page(page, {"1:1", "2:1"})
    assert "1:1" not in result.frames
    assert "2:1" in result.frames


def test_scan_page_totals_match_frame_sums():
    """INVARIANT: page-level totals equal the sum of all per-frame counts."""
    raw_fill, _ = _solid_fill(RED_COLOR)
    frame_a = _frame(node_id="1:1", children=[_rect(fills=[raw_fill])])
    frame_b = _frame(node_id="2:1", children=[_rect(fills=[raw_fill])])
    page = _page([frame_a, frame_b])

    result = scan_page(page, {"1:1", "2:1"})
    frame_raw_total = sum(f.raw for f in result.frames.values())
    assert result.raw == frame_raw_total


def test_scan_page_skips_frames_not_in_frame_ids():
    """INVARIANT: frames not in frame_ids are not scanned."""
    raw_fill, _ = _solid_fill(RED_COLOR)
    frame = _frame(node_id="1:1", children=[_rect(fills=[raw_fill])])
    page = _page([frame])

    result = scan_page(page, set())  # empty — no frames requested
    assert result.raw == 0
    assert result.frames == {}


def test_scan_page_frames_inside_sections():
    """INVARIANT: scan_page finds frames nested inside SECTION nodes."""
    raw_fill, _ = _solid_fill(RED_COLOR)
    frame = _frame(node_id="1:1", children=[_rect(fills=[raw_fill])])
    section = {"id": "0:99", "name": "Section", "type": "SECTION", "children": [frame]}
    page = _page([section])

    result = scan_page(page, {"1:1"})
    assert "1:1" in result.frames
    assert result.raw == 1


def test_scan_page_component_node_type_is_scanned():
    """INVARIANT: COMPONENT nodes (not just FRAME) in frame_ids are scanned."""
    raw_fill, _ = _solid_fill(RED_COLOR)
    comp = {"id": "1:1", "name": "Comp", "type": "COMPONENT", "children": [_rect(fills=[raw_fill])]}
    page = _page([comp])

    result = scan_page(page, {"1:1"})
    assert result.raw == 1
    assert "1:1" in result.frames


# --- edge cases: fills_bv / cornerRadius / styles.text ---

def test_scan_frame_fills_bv_shorter_than_fills():
    """INVARIANT: if fills_bv has fewer entries than fills, the unpaired fills are raw."""
    fill_a, bv_a = _solid_fill(RED_COLOR, var_id=DS_VAR_ID)  # index 0 — valid
    fill_b, _ = _solid_fill(DARK_COLOR)                       # index 1 — raw (no bv entry)
    # fills has 2 entries, fills_bv has only 1
    node = _rect(node_id="2:1", fills=[fill_a, fill_b], fills_bv=[bv_a])
    frame = _frame(children=[node])
    result = scan_frame(frame)

    assert result.valid == 1
    assert result.raw == 1
    assert result.issues[0].index == 1  # the second fill is the raw one


def test_scan_frame_corner_radius_mixed_is_skipped():
    """INVARIANT: cornerRadius='mixed' (individual corners set) is not classified."""
    node = _rect(node_id="2:1")
    node["cornerRadius"] = "mixed"
    frame = _frame(children=[node])
    result = scan_frame(frame)

    cr_issues = [i for i in result.issues if i.property == "cornerRadius"]
    assert cr_issues == []


def test_scan_frame_styles_text_covers_font_props():
    """INVARIANT: styles.text (API-style text style reference) marks all font props valid."""
    text = _text(font_size=16.0, font_family="Figtree", font_weight=400.0)
    text["styles"] = {"text": "S:body-style"}  # API-style, no textStyleId key
    frame = _frame(children=[text])
    result = scan_frame(frame)

    assert result.raw == 0
    assert result.valid == 3
    assert result.issues == []
