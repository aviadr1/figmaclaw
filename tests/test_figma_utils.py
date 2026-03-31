"""Tests for figma_utils.py.

INVARIANTS:
- parse_team_id_from_url extracts numeric team ID from Figma team URLs
- parse_team_id_from_url returns the input unchanged for bare IDs
- parse_since converts duration strings into past UTC datetimes
- parse_since raises ValueError for unrecognised formats
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from figmaclaw.figma_utils import parse_since, parse_team_id_from_url


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
        delta = datetime.now(timezone.utc) - result
        assert 6 < delta.days < 8

    def test_weeks(self):
        """INVARIANT: '2w' produces a datetime approximately 14 days in the past."""
        result = parse_since("2w")
        delta = datetime.now(timezone.utc) - result
        assert 13 < delta.days < 15

    def test_months(self):
        """INVARIANT: '3m' produces a datetime approximately 90 days in the past."""
        result = parse_since("3m")
        delta = datetime.now(timezone.utc) - result
        assert 89 < delta.days < 91

    def test_years(self):
        """INVARIANT: '1y' produces a datetime approximately 365 days in the past."""
        result = parse_since("1y")
        delta = datetime.now(timezone.utc) - result
        assert 364 < delta.days < 366

    def test_result_is_timezone_aware(self):
        """INVARIANT: parse_since always returns a timezone-aware UTC datetime."""
        result = parse_since("1d")
        assert result.tzinfo is not None

    def test_invalid_format_raises_value_error(self):
        """INVARIANT: Unrecognised duration strings raise ValueError."""
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_since("3months")

    def test_missing_unit_raises_value_error(self):
        """INVARIANT: A bare number without a unit suffix raises ValueError."""
        with pytest.raises(ValueError):
            parse_since("30")
