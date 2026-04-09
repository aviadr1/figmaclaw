"""Tests for _write_token_sidecar idempotency and schema v2 aggregation.

INVARIANTS:
- _write_token_sidecar writes the correct structure on first call
- _write_token_sidecar does NOT modify the file when token data is unchanged
  (only generated_at would differ — suppressed to avoid spurious git commits)
- _write_token_sidecar DOES update the file when token data changes
- Schema v2: issues are aggregated by (property, classification, value) with count
"""

from __future__ import annotations

import json
from pathlib import Path

from figmaclaw.pull_logic import _write_token_sidecar
from figmaclaw.token_scan import FrameTokenScan, PageTokenScan, TokenIssue


def _make_token_scan(raw: int = 2, stale: int = 1) -> PageTokenScan:
    issue = TokenIssue(
        node_id="11:1",
        node_name="bg",
        node_type="RECTANGLE",
        node_path=["intro", "bg"],
        property="fill",
        classification="raw",
        current_value={"r": 1.0, "g": 0.0, "b": 0.0},
        hex="#FF0000",
    )
    frame_scan = FrameTokenScan(name="welcome", raw=raw, stale=stale, valid=0, issues=[issue])
    return PageTokenScan(raw=raw, stale=stale, valid=0, frames={"11:1": frame_scan})


def test_write_token_sidecar_creates_file_with_correct_structure(tmp_path: Path):
    """INVARIANT: sidecar contains schema_version, file_key, page_node_id, generated_at, summary, frames."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc123\n---\n")
    token_scan = _make_token_scan()

    _write_token_sidecar(screen_md, "abc123", "7741:45837", token_scan)

    sidecar = tmp_path / "page.tokens.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["schema_version"] == 2
    assert data["file_key"] == "abc123"
    assert data["page_node_id"] == "7741:45837"
    assert "generated_at" in data
    assert data["summary"] == {"raw": 2, "stale": 1, "valid": 0}
    assert "11:1" in data["frames"]
    assert data["frames"]["11:1"]["name"] == "welcome"
    assert len(data["frames"]["11:1"]["issues"]) == 1
    assert data["frames"]["11:1"]["issues"][0]["count"] == 1


def test_write_token_sidecar_is_idempotent_when_data_unchanged(tmp_path: Path):
    """INVARIANT: repeated calls with identical token data must not modify the file.

    Before the fix, every pull run rewrote .tokens.json with a new generated_at
    timestamp, creating spurious git commits that triggered Claude enrichment.
    After the fix, only token data changes cause a write.
    """
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc123\n---\n")
    token_scan = _make_token_scan()

    _write_token_sidecar(screen_md, "abc123", "7741:45837", token_scan)
    sidecar = tmp_path / "page.tokens.json"
    content_after_first = sidecar.read_text()
    mtime_after_first = sidecar.stat().st_mtime_ns

    _write_token_sidecar(screen_md, "abc123", "7741:45837", token_scan)
    content_after_second = sidecar.read_text()
    mtime_after_second = sidecar.stat().st_mtime_ns

    # File must not be rewritten — same content and same mtime
    assert content_after_first == content_after_second
    assert mtime_after_first == mtime_after_second


def test_write_token_sidecar_updates_file_when_raw_count_changes(tmp_path: Path):
    """INVARIANT: when token counts differ, sidecar IS updated (change detection still works)."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc123\n---\n")

    _write_token_sidecar(screen_md, "abc123", "7741:45837", _make_token_scan(raw=2, stale=1))
    sidecar = tmp_path / "page.tokens.json"
    content_before = sidecar.read_text()

    _write_token_sidecar(screen_md, "abc123", "7741:45837", _make_token_scan(raw=5, stale=0))
    content_after = sidecar.read_text()

    assert content_before != content_after
    data = json.loads(content_after)
    assert data["summary"]["raw"] == 5
    assert data["summary"]["stale"] == 0


def test_write_token_sidecar_writes_on_empty_scan_after_populated(tmp_path: Path):
    """INVARIANT: switching from populated scan to empty scan updates the file."""
    screen_md = tmp_path / "page.md"
    screen_md.write_text("---\nfile_key: abc123\n---\n")

    _write_token_sidecar(screen_md, "abc123", "7741:45837", _make_token_scan(raw=3))
    sidecar = tmp_path / "page.tokens.json"

    empty_scan = PageTokenScan()
    _write_token_sidecar(screen_md, "abc123", "7741:45837", empty_scan)

    data = json.loads(sidecar.read_text())
    assert data["summary"]["raw"] == 0
    assert data["frames"] == {}
