"""Tests for commands/write_descriptions.py.

INVARIANTS:
- write-descriptions updates only the description cell of matched frame rows
- write-descriptions preserves all non-matched rows exactly
- write-descriptions preserves frontmatter, page summary, section intros
- write-descriptions handles missing node_ids gracefully (warning, not error)
- write-descriptions escapes pipe characters in descriptions
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from figmaclaw.commands.write_descriptions import _update_descriptions
from figmaclaw.main import cli

_TEST_MD = """\
---
file_key: abc
page_node_id: '1:1'
frames: ['11:1', '11:2', '21:1']
---

# File / Page

[Open in Figma](https://figma.com)

Page summary text.

## Auth (`10:1`)

Auth section intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | (no description yet) |
| Signup | `11:2` | (no description yet) |

## Dashboard (`20:1`)

Dashboard intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Home | `21:1` | (no description yet) |

## Screen flows

```mermaid
flowchart LR
    A["Login"] --> B["Home"]
```
"""


def test_update_descriptions_replaces_matched_rows() -> None:
    """INVARIANT: matched node_ids get their descriptions updated."""
    result, count, _ = _update_descriptions(
        _TEST_MD,
        {
            "11:1": "A login screen with email/password fields",
            "21:1": "The main dashboard with activity feed",
        },
    )
    assert count == 2
    assert "A login screen with email/password fields" in result
    assert "The main dashboard with activity feed" in result
    # Unmatched row still has placeholder
    assert "| Signup | `11:2` | (no description yet) |" in result


def test_update_descriptions_preserves_unmatched_rows() -> None:
    """INVARIANT: rows not in descriptions dict are unchanged."""
    result, count, _ = _update_descriptions(_TEST_MD, {"11:1": "Updated"})
    assert count == 1
    assert "| Signup | `11:2` | (no description yet) |" in result
    assert "| Home | `21:1` | (no description yet) |" in result


def test_update_descriptions_preserves_frontmatter() -> None:
    """INVARIANT: frontmatter is never touched."""
    result, _, _ = _update_descriptions(_TEST_MD, {"11:1": "New desc"})
    assert "file_key: abc" in result
    assert "frames: ['11:1', '11:2', '21:1']" in result


def test_update_descriptions_preserves_page_summary() -> None:
    """INVARIANT: page summary is never touched."""
    result, _, _ = _update_descriptions(_TEST_MD, {"11:1": "New desc"})
    assert "Page summary text." in result


def test_update_descriptions_preserves_section_intros() -> None:
    """INVARIANT: section intros are never touched."""
    result, _, _ = _update_descriptions(_TEST_MD, {"11:1": "New desc"})
    assert "Auth section intro." in result
    assert "Dashboard intro." in result


def test_update_descriptions_preserves_screen_flows() -> None:
    """INVARIANT: mermaid block is never touched."""
    result, _, _ = _update_descriptions(_TEST_MD, {"11:1": "New desc"})
    assert "```mermaid" in result
    assert 'A["Login"] --> B["Home"]' in result


def test_update_descriptions_escapes_pipe() -> None:
    """INVARIANT: pipe characters in descriptions are escaped."""
    result, count, _ = _update_descriptions(
        _TEST_MD,
        {
            "11:1": "A screen with yes | no options",
        },
    )
    assert count == 1
    assert "yes \\| no" in result


def test_update_descriptions_missing_node_id() -> None:
    """INVARIANT: missing node_ids are silently skipped (count reflects actual updates)."""
    result, count, _ = _update_descriptions(
        _TEST_MD,
        {
            "99:99": "Ghost description",
        },
    )
    assert count == 0


def test_update_descriptions_partial_match() -> None:
    """INVARIANT: only matched rows updated, count reflects actual updates."""
    result, count, _ = _update_descriptions(
        _TEST_MD,
        {
            "11:1": "Updated login",
            "99:99": "Ghost",
        },
    )
    assert count == 1
    assert "Updated login" in result


def test_cli_write_descriptions(tmp_path: Path) -> None:
    """CLI integration: write-descriptions updates rows."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_TEST_MD)

    descs = json.dumps({"11:1": "CLI test description", "11:2": "Signup form"})
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "write-descriptions",
            str(md_path),
            "--descriptions",
            descs,
        ],
    )
    assert result.exit_code == 0
    assert "updated 2/2" in result.output

    updated = md_path.read_text()
    assert "CLI test description" in updated
    assert "Signup form" in updated
    # Page summary and mermaid untouched
    assert "Page summary text." in updated
    assert "```mermaid" in updated


def test_cli_write_descriptions_warns_on_missing(tmp_path: Path) -> None:
    """CLI integration: warns about node_ids not found in table."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_TEST_MD)

    descs = json.dumps({"99:99": "Ghost"})
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "write-descriptions",
            str(md_path),
            "--descriptions",
            descs,
        ],
    )
    assert result.exit_code == 0
    assert "updated 0/1" in result.output


def test_update_descriptions_idempotent() -> None:
    """INVARIANT: running twice with same descriptions produces identical output."""
    descs = {"11:1": "A login screen", "11:2": "A signup screen"}
    result1, _, _ = _update_descriptions(_TEST_MD, descs)
    result2, _, _ = _update_descriptions(result1, descs)
    assert result1 == result2


def test_update_descriptions_handles_failed_frames() -> None:
    """INVARIANT: failed frames (screenshot unavailable) clear the placeholder."""
    result, count, _ = _update_descriptions(
        _TEST_MD,
        {
            "11:1": "(screenshot unavailable)",
        },
    )
    assert count == 1
    assert "(screenshot unavailable)" in result
    assert "| Login | `11:1` | (screenshot unavailable) |" in result
    # This frame no longer has the canonical placeholder, but remains unresolved
    # and retryable because of the unavailable marker.
    assert result.count("(no description yet)") == 2  # 11:2 and 21:1 still pending


def test_update_descriptions_clears_all_placeholders_with_mixed() -> None:
    """INVARIANT: mix of real descriptions and unavailable markers works."""
    result, count, _ = _update_descriptions(
        _TEST_MD,
        {
            "11:1": "A real login screen description",
            "11:2": "(screenshot unavailable)",
            "21:1": "The dashboard",
        },
    )
    assert count == 3
    assert "(no description yet)" not in result


def test_update_descriptions_does_not_touch_non_canonical_table_rows() -> None:
    """INVARIANT: replacement is limited to canonical frame/variant section tables."""
    md = """\
---
file_key: abc
page_node_id: '1:1'
frames: ['11:1']
---

# Page

## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | (no description yet) |

## Notes (`20:1`)

| Title | Node ID | Value |
|-------|---------|-------|
| Debug row | `11:1` | should stay untouched |
"""

    result, count, _ = _update_descriptions(md, {"11:1": "Updated canonical row"})

    assert count == 1
    assert "| Login | `11:1` | Updated canonical row |" in result
    assert "| Debug row | `11:1` | should stay untouched |" in result


def test_cli_write_descriptions_invalid_json_is_usage_error(tmp_path: Path) -> None:
    """Invalid JSON input must fail cleanly without Python traceback."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_TEST_MD)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "write-descriptions",
            str(md_path),
            "--descriptions",
            '{"11:1": "ok",',
        ],
    )

    assert result.exit_code != 0
    assert "Invalid JSON" in result.output
    assert "Traceback" not in result.output


def test_cli_write_descriptions_supports_descriptions_file(tmp_path: Path) -> None:
    """Large payload mode: JSON from file should update rows successfully."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_TEST_MD)

    payload = tmp_path / "descriptions.json"
    payload.write_text(json.dumps({"11:1": "From file", "11:2": "Also from file"}))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "write-descriptions",
            str(md_path),
            "--descriptions-file",
            str(payload),
        ],
    )

    assert result.exit_code == 0
    assert "updated 2/2" in result.output
    updated = md_path.read_text()
    assert "From file" in updated
    assert "Also from file" in updated


def test_cli_write_descriptions_rejects_non_string_values(tmp_path: Path) -> None:
    """Descriptions JSON values must be strings for deterministic markdown output."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_TEST_MD)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "write-descriptions",
            str(md_path),
            "--descriptions",
            '{"11:1": 123}',
        ],
    )

    assert result.exit_code != 0
    assert "must be a string" in result.output


def test_cli_write_descriptions_rejects_dual_input_modes(tmp_path: Path) -> None:
    """CLI should reject ambiguous payload source selection."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_TEST_MD)

    payload = tmp_path / "descriptions.json"
    payload.write_text('{"11:1": "From file"}')

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "write-descriptions",
            str(md_path),
            "--descriptions",
            '{"11:1": "inline"}',
            "--descriptions-file",
            str(payload),
        ],
    )

    assert result.exit_code != 0
    assert "Use either --descriptions or --descriptions-file" in result.output
