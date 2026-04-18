"""Parametric invariant: every selection/entry boundary heals stuck files.

Issue: figmaclaw#123 — structural invariants must heal on encounter.

The bug shape this test pins: a file with known invariant violations
(orphan enriched_frame_hashes, orphan body rows, missing
enriched_schema_version) must be healed on the very next invocation of
any entry point that reads it. Without this, a reader-only path
(claude-run selection, inspect) can encounter stuck state hour after
hour and never trigger the healing logic that lives in write paths.

Adding a new selection/entry boundary later? Add it to
``_HEALING_ENTRY_POINTS`` below and the test will automatically
exercise it. If your new entry point doesn't call
``normalize_page_file`` (or equivalent), this test fails and you're
told to wire it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from figmaclaw.commands.claude_run import enrichment_info
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.normalize import normalize_page_file

# A "stuck" fixture: exactly the shape of the linear-git incident files
# — orphan enriched_frame_hashes entry, orphan body row, missing
# schema_version.
_STUCK_FIXTURE = """---
file_key: fk
page_node_id: '1:1'
frames: ['11:1']
enriched_frame_hashes: {'11:1': aaaaaaaa, 'DEAD:1': deadbeef}
---

# Page

## Section (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Kept | `11:1` | Real description. |
| Ghost | `DEAD:1` | (no screenshot available) |
"""


def _invoke_normalize(md: Path) -> None:
    """Direct call — baseline for what "healing" means."""
    normalize_page_file(md)


def _invoke_enrichment_info(md: Path) -> None:
    """claude-run selection path. Calls normalize_page_file internally."""
    enrichment_info(md)


# Single source of truth for entry points under test. When a new
# selection/entry boundary is added to figmaclaw, list it here. The
# registered callable must leave any invariant violations healed after
# its first invocation — either by calling normalize_page_file
# directly, or by being downstream of something that does.
_HEALING_ENTRY_POINTS: list[tuple[str, Callable[[Path], None]]] = [
    ("normalize_page_file", _invoke_normalize),
    ("enrichment_info", _invoke_enrichment_info),
    # Future entries go here. Examples:
    # ("pull._pull_file_gate", _invoke_pull_gate),
    # ("inspect_cmd", _invoke_inspect),
]


def _assert_healed(md: Path) -> None:
    """Shared post-conditions: the stuck fixture is resolved."""
    fm = parse_frontmatter(md.read_text())
    assert fm is not None
    # Invariant 1: schema_version present.
    assert fm.enriched_schema_version is not None
    # Invariant 2: frame-keyed dict orphan pruned.
    assert "DEAD:1" not in fm.enriched_frame_hashes
    assert set(fm.enriched_frame_hashes.keys()) <= set(fm.frames)
    # Invariant 3: body orphan row dropped.
    assert "`DEAD:1`" not in md.read_text()
    # Valid content preserved.
    assert "`11:1`" in md.read_text()
    assert "Real description." in md.read_text()


@pytest.mark.parametrize(
    "entry_name,entry_call",
    _HEALING_ENTRY_POINTS,
    ids=[name for name, _ in _HEALING_ENTRY_POINTS],
)
def test_entry_point_heals_stuck_fixture_on_first_encounter(
    tmp_path: Path,
    entry_name: str,
    entry_call: Callable[[Path], None],
) -> None:
    """Every registered entry point must leave a stuck file in a
    healed state after one invocation.

    This is the contract that closes figmaclaw#123's category. A future
    PR that adds a new selection path must either register it here (and
    prove it heals) or explicitly acknowledge that the path does NOT
    heal — in which case we need a clear rationale and a stable "that's
    OK for this path" comment.
    """
    md = tmp_path / "stuck.md"
    md.write_text(_STUCK_FIXTURE)

    entry_call(md)

    _assert_healed(md)


@pytest.mark.parametrize(
    "entry_name,entry_call",
    _HEALING_ENTRY_POINTS,
    ids=[name for name, _ in _HEALING_ENTRY_POINTS],
)
def test_entry_point_is_idempotent_on_already_healed_file(
    tmp_path: Path,
    entry_name: str,
    entry_call: Callable[[Path], None],
) -> None:
    """After the first healing call, subsequent calls must be byte-for-byte
    no-ops. Guards against accidental re-serialization side effects
    (e.g. YAML ordering drift) and preserves the "idempotency per
    CLAUDE.md" rule for any entry-point wire.
    """
    md = tmp_path / "stuck.md"
    md.write_text(_STUCK_FIXTURE)

    # First call heals (or observes that the file is already clean).
    entry_call(md)
    canonical = md.read_text()
    mtime_after_first = md.stat().st_mtime_ns

    # Second call must leave the file byte-for-byte unchanged.
    entry_call(md)

    assert md.read_text() == canonical
    assert md.stat().st_mtime_ns == mtime_after_first


def test_registered_entry_point_list_is_non_empty() -> None:
    """Loud failure if someone accidentally empties _HEALING_ENTRY_POINTS.

    The list IS the category invariant — dropping entries without
    replacement would silently remove the guarantee.
    """
    assert _HEALING_ENTRY_POINTS, (
        "_HEALING_ENTRY_POINTS must register at least normalize_page_file + "
        "every selection/entry boundary in figmaclaw that reads a page file."
    )
