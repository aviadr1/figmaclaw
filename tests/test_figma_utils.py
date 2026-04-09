"""Tests for figma_utils.py.

INVARIANTS:
- parse_team_id_from_url extracts numeric team ID from Figma team URLs
- parse_team_id_from_url returns the input unchanged for bare IDs
- parse_since converts duration strings into past UTC datetimes
- parse_since raises ValueError for unrecognised formats
- write_json_if_changed writes on first call and returns True
- write_json_if_changed skips write (same content, same mtime) when only ignored keys differ
- write_json_if_changed writes and returns True when non-ignored content changes
- write_json_if_changed creates parent directories automatically
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from figmaclaw.figma_utils import parse_since, parse_team_id_from_url, write_json_if_changed


class TestParseTeamIdFromUrl:
    def test_extracts_id_from_full_url(self):
        """INVARIANT: Full Figma team URL yields the numeric team ID."""
        url = "https://www.figma.com/files/team/1314617533998771588/Gigaverse"
        assert parse_team_id_from_url(url) == "1314617533998771588"

    def test_extracts_id_from_url_without_trailing_path(self):
        """INVARIANT: URL ending at the team ID still parses correctly."""
        url = "https://www.figma.com/files/team/9999000011112222"
        assert parse_team_id_from_url(url) == "9999000011112222"

    def test_returns_bare_id_unchanged(self):
        """INVARIANT: A bare numeric string is returned as-is (already the team ID)."""
        assert parse_team_id_from_url("1314617533998771588") == "1314617533998771588"

    def test_strips_whitespace_from_bare_id(self):
        """INVARIANT: Leading/trailing whitespace is stripped from bare IDs."""
        assert parse_team_id_from_url("  123456  ") == "123456"


class TestParseSince:
    def test_days(self):
        """INVARIANT: '7d' produces a datetime approximately 7 days in the past."""
        result = parse_since("7d")
        assert result is not None
        delta = datetime.now(timezone.utc) - result
        assert 6 < delta.days < 8

    def test_weeks(self):
        """INVARIANT: '2w' produces a datetime approximately 14 days in the past."""
        result = parse_since("2w")
        assert result is not None
        delta = datetime.now(timezone.utc) - result
        assert 13 < delta.days < 15

    def test_months(self):
        """INVARIANT: '3m' produces a datetime approximately 90 days in the past."""
        result = parse_since("3m")
        assert result is not None
        delta = datetime.now(timezone.utc) - result
        assert 89 < delta.days < 91

    def test_years(self):
        """INVARIANT: '1y' produces a datetime approximately 365 days in the past."""
        result = parse_since("1y")
        assert result is not None
        delta = datetime.now(timezone.utc) - result
        assert 364 < delta.days < 366

    def test_result_is_timezone_aware(self):
        """INVARIANT: parse_since always returns a timezone-aware UTC datetime."""
        result = parse_since("1d")
        assert result is not None
        assert result.tzinfo is not None

    def test_invalid_format_raises_value_error(self):
        """INVARIANT: Unrecognised duration strings raise ValueError."""
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_since("3months")

    def test_missing_unit_raises_value_error(self):
        """INVARIANT: A bare number without a unit suffix raises ValueError."""
        with pytest.raises(ValueError):
            parse_since("30")


class TestWriteJsonIfChanged:
    def test_writes_file_on_first_call_and_returns_true(self, tmp_path: Path):
        """INVARIANT: write_json_if_changed creates the file and returns True when it does not exist."""
        path = tmp_path / "out.json"
        written = write_json_if_changed(path, {"a": 1})
        assert written is True
        assert path.exists()
        assert json.loads(path.read_text()) == {"a": 1}

    def test_skips_write_when_only_ignored_key_differs(self, tmp_path: Path):
        """INVARIANT: write_json_if_changed returns False and does not touch the file when
        only ignored keys (e.g. timestamps) would change.

        This is the core contract that prevents spurious git commits every pull run.
        """
        path = tmp_path / "out.json"
        write_json_if_changed(path, {"data": "x", "ts": "2026-01-01"}, ignore_keys=frozenset({"ts"}))
        mtime_first = path.stat().st_mtime_ns
        content_first = path.read_text()

        written = write_json_if_changed(path, {"data": "x", "ts": "2099-12-31"}, ignore_keys=frozenset({"ts"}))

        assert written is False
        assert path.stat().st_mtime_ns == mtime_first
        assert path.read_text() == content_first

    def test_writes_when_non_ignored_content_changes(self, tmp_path: Path):
        """INVARIANT: write_json_if_changed returns True and updates the file when payload data changes."""
        path = tmp_path / "out.json"
        write_json_if_changed(path, {"data": "old", "ts": "2026-01-01"}, ignore_keys=frozenset({"ts"}))

        written = write_json_if_changed(path, {"data": "new", "ts": "2026-01-01"}, ignore_keys=frozenset({"ts"}))

        assert written is True
        assert json.loads(path.read_text())["data"] == "new"

    def test_creates_parent_directories(self, tmp_path: Path):
        """INVARIANT: write_json_if_changed creates missing parent directories."""
        path = tmp_path / "deep" / "nested" / "out.json"
        write_json_if_changed(path, {"x": 1})
        assert path.exists()

    def test_no_ignore_keys_compares_full_content(self, tmp_path: Path):
        """INVARIANT: without ignore_keys, identical content skips the write."""
        path = tmp_path / "out.json"
        write_json_if_changed(path, {"a": 1})
        mtime_first = path.stat().st_mtime_ns

        written = write_json_if_changed(path, {"a": 1})

        assert written is False
        assert path.stat().st_mtime_ns == mtime_first

    def test_overwrites_corrupt_existing_file(self, tmp_path: Path):
        """INVARIANT: if the existing file cannot be parsed, write_json_if_changed writes unconditionally."""
        path = tmp_path / "out.json"
        path.write_text("not valid json")

        written = write_json_if_changed(path, {"a": 1})

        assert written is True
        assert json.loads(path.read_text()) == {"a": 1}
