"""Tests for figmaclaw.body_validation.

INVARIANTS:
- body rows must contain exactly frontmatter frames (no missing, no extras, no duplicates)
- parser ignores prose-only sections when collecting frame table rows
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
