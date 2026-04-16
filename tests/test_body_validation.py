"""Tests for figmaclaw.body_validation.

INVARIANTS:
- body rows must contain exactly frontmatter frames (no missing, no extras, no duplicates)
- parser only reads canonical frame tables in frame sections
- parser ignores non-frame tables and fenced table-like content
- duplicate frontmatter frame IDs are invalid
"""

from __future__ import annotations

from figmaclaw.body_validation import validate_body_against_frames


def test_validate_body_against_frames_ok() -> None:
    body = """\
## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | desc |
| Signup | `11:2` | desc |
"""
    result = validate_body_against_frames(body, ["11:1", "11:2"])
    assert result.ok
    assert result.messages() == []


def test_validate_body_against_frames_missing_extra_duplicate() -> None:
    body = """\
## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | desc |
| Login Copy | `11:1` | dup |
| Unknown | `99:1` | extra |
"""
    result = validate_body_against_frames(body, ["11:1", "11:2"])
    assert not result.ok
    assert result.missing_node_ids == ["11:2"]
    assert result.extra_node_ids == ["99:1"]
    assert result.duplicate_node_ids == ["11:1"]


def test_validate_body_ignores_prose_section_tables() -> None:
    body = """\
## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | desc |

## Screen Flow

| Fake | Node ID | Notes |
|------|---------|-------|
| Not a frame row | `99:1` | prose table |
"""
    result = validate_body_against_frames(body, ["11:1"])
    assert result.ok


def test_validate_body_ignores_non_canonical_tables_inside_frame_sections() -> None:
    body = """\
## Auth (`10:1`)

| Notes | Node ID | Value |
|------|---------|-------|
| diagnostic row | `99:1` | this is not a frame table |

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | desc |
"""
    result = validate_body_against_frames(body, ["11:1"])
    assert result.ok


def test_validate_body_ignores_table_like_rows_in_fenced_code_block() -> None:
    body = """\
## Auth (`10:1`)

```markdown
| Screen | Node ID | Description |
|--------|---------|-------------|
| Fake | `99:1` | from docs |
```

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | desc |
"""
    result = validate_body_against_frames(body, ["11:1"])
    assert result.ok


def test_validate_body_rejects_duplicate_frontmatter_frame_ids() -> None:
    body = """\
## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | desc |
"""
    result = validate_body_against_frames(body, ["11:1", "11:1"])
    assert not result.ok
    assert result.duplicate_frontmatter_node_ids == ["11:1"]
