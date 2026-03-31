"""Shared utilities for figmaclaw commands."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone


def make_anthropic_client() -> object | None:
    """Create an AsyncAnthropic client if ANTHROPIC_API_KEY is set, else None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=api_key)


def parse_team_id_from_url(url_or_id: str) -> str:
    """Extract a Figma team ID from a URL or return the string as-is.

    Handles:
      https://www.figma.com/files/team/1314617533998771588/...
      https://www.figma.com/files/team/1314617533998771588
      1314617533998771588
    """
    m = re.search(r"/team/(\d+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


_SINCE_RE = re.compile(r"^(\d+)([dwmy])$")


def parse_since(since: str) -> datetime:
    """Parse a human duration like '3m', '7d', '1y' into a UTC datetime (now - duration).

    Supported suffixes: d (days), w (weeks), m (months≈30d), y (years≈365d).
    Raises ValueError for unrecognised formats.
    """
    m = _SINCE_RE.match(since.strip())
    if not m:
        raise ValueError(f"Cannot parse --since {since!r}. Use e.g. '3m', '7d', '1y'.")
    n, unit = int(m.group(1)), m.group(2)
    days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
    return datetime.now(timezone.utc) - timedelta(days=days)
