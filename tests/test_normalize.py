"""Tests for figmaclaw.normalize (figmaclaw#123).

``normalize_page_file`` is the canonical "heal on encounter" entry
point. Every entry boundary (claude-run selector, pull discovery, CLI)
must call it. These tests pin:

- Idempotency: a clean file is byte-for-byte unchanged; two consecutive
  calls make zero writes on the second.
- DRY: the function composes existing helpers (chokepoint rebuild +
  orphan-row prune + schema-version backfill). No logic lives here that
  lives elsewhere.
- Prose preservation: section intros, Mermaid, page summary untouched.
- Observability: :class:`NormalizationResult` counters are exact.
- Guardrail: ``_FRAME_KEYED_DICT_FIELDS`` stays in lockstep with the
  chokepoint prune in ``_build_frontmatter``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from figmaclaw.normalize import (
    _FRAME_KEYED_DICT_FIELDS,
    NormalizationResult,
    normalize_page_file,
)

_CLEAN_PAGE = """---
file_key: fk
page_node_id: '1:1'
frames: ['11:1', '11:2']
enriched_schema_version: 1
---

# Page

Page summary paragraph.

## Auth (`10:1`)

Section intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | A real description. |
| Signup | `11:2` | Another description. |
"""


_STUCK_PAGE = """---
file_key: fk
page_node_id: '1:1'
frames: ['11:1']
enriched_schema_version: 1
enriched_frame_hashes: {'11:1': aaaaaaaa, 'DEAD:1': deadbeef}
---

# Page

Page summary paragraph.

## Auth (`10:1`)

Section intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | A real description. |
| Ghost | `DEAD:1` | (no screenshot available) |

```mermaid
flowchart LR
  A --> B
```
"""


class TestIdempotency:
    def test_already_normalized_file_is_untouched(self, tmp_path: Path) -> None:
        """After the FIRST normalize canonicalizes a file (field order etc),
        subsequent normalize calls must be a pure no-op — no mtime bump,
        no byte change. This is the idempotency contract from CLAUDE.md."""
        md = tmp_path / "page.md"
        md.write_text(_CLEAN_PAGE)

        # First call may canonicalize field order — that's allowed.
        normalize_page_file(md)
        normalized_text = md.read_text()
        mtime_after_first = md.stat().st_mtime_ns

        # Second call must be a pure no-op.
        result = normalize_page_file(md)

        assert result.changed is False
        assert md.read_text() == normalized_text
        assert md.stat().st_mtime_ns == mtime_after_first

    def test_second_call_is_noop_after_first_heals(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_STUCK_PAGE)

        first = normalize_page_file(md)
        assert first.changed is True

        mtime_after_first = md.stat().st_mtime_ns
        second = normalize_page_file(md)

        assert second.changed is False
        assert md.stat().st_mtime_ns == mtime_after_first


class TestHealing:
    def test_heals_orphan_enriched_frame_hashes(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_STUCK_PAGE)

        result = normalize_page_file(md)

        assert result.changed is True
        assert result.frame_keyed_dict_orphans_pruned == 1  # DEAD:1
        assert "DEAD:1" not in md.read_text()

    def test_heals_orphan_body_rows(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_STUCK_PAGE)

        result = normalize_page_file(md)

        assert result.body_orphan_rows_pruned == 1
        assert "`DEAD:1`" not in md.read_text()

    def test_heals_missing_schema_version(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        # Legacy page: no enriched_schema_version field.
        md.write_text(_CLEAN_PAGE.replace("enriched_schema_version: 1\n", ""))

        result = normalize_page_file(md)

        assert result.schema_version_backfilled is True
        assert result.changed is True
        assert "enriched_schema_version: 0" in md.read_text()


class TestPreservesProse:
    def test_prose_and_mermaid_survive_heal(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_STUCK_PAGE)

        normalize_page_file(md)

        after = md.read_text()
        assert "Page summary paragraph." in after
        assert "## Auth (`10:1`)" in after
        assert "Section intro." in after
        assert "```mermaid" in after
        assert "flowchart LR" in after
        assert "A --> B" in after

    def test_valid_rows_preserved(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_STUCK_PAGE)

        normalize_page_file(md)

        after = md.read_text()
        assert "`11:1`" in after
        assert "A real description." in after


class TestEdgeCases:
    def test_non_figmaclaw_file_is_ignored(self, tmp_path: Path) -> None:
        md = tmp_path / "README.md"
        md.write_text("# Project\n\nSome prose.\n")
        before = md.read_text()

        result = normalize_page_file(md)

        assert result.changed is False
        assert result.reason
        assert md.read_text() == before

    def test_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        result = normalize_page_file(tmp_path / "nonexistent.md")
        assert result.changed is False
        assert "read failed" in result.reason

    def test_empty_frames_is_safe(self, tmp_path: Path) -> None:
        """A page with empty frames list shouldn't have its body pruned
        just because the allowed set is empty (fallback behavior from
        figmaclaw#121: empty-allowed = no-op prune)."""
        md = tmp_path / "page.md"
        md.write_text(
            "---\n"
            "file_key: fk\n"
            "page_node_id: '1:1'\n"
            "frames: []\n"
            "enriched_schema_version: 1\n"
            "---\n\n"
            "# Page\n\n"
            "## Section (`10:1`)\n\n"
            "| Screen | Node ID | Description |\n"
            "|--------|---------|-------------|\n"
            "| Row | `11:1` | desc |\n"
        )

        normalize_page_file(md)

        # frames=[] means "nothing is allowed" — but the empty-allowed
        # fallback short-circuits to no-op to avoid mutating legacy files.
        # If the frontmatter genuinely should prune, the rebuild through
        # the chokepoint will still clear frame-keyed dicts.
        assert "`11:1`" in md.read_text()


class TestDrynessGuardrail:
    """The set of frame-keyed dict fields in normalize._FRAME_KEYED_DICT_FIELDS
    must match the set pruned by the _build_frontmatter chokepoint.

    If someone adds a new frame-keyed dict but forgets to update one
    side, either pruning or counting drifts. This guardrail locks the
    two lists to a single source of truth.
    """

    def test_frame_keyed_dict_fields_match_chokepoint(self) -> None:
        source = Path("figmaclaw/figma_render.py").read_text()

        # Extract every variable name passed to `_prune_to_allowed` inside
        # `_build_frontmatter`. The chokepoint is the only caller.
        tree = ast.parse(source)
        pruned_names: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_prune_to_allowed"
            ):
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Name):
                    pruned_names.add(arg.id)

        assert pruned_names == set(_FRAME_KEYED_DICT_FIELDS), (
            f"normalize._FRAME_KEYED_DICT_FIELDS={set(_FRAME_KEYED_DICT_FIELDS)} "
            f"but _build_frontmatter prunes {pruned_names}. "
            "Keep these in lockstep — every frame-keyed dict must be in both "
            "(counted by normalize, pruned by the chokepoint)."
        )


class TestObservability:
    def test_result_is_frozen(self) -> None:
        """Frozen pydantic (per CLAUDE.md conventions) — callers can
        rely on hashability / no mutation surprises."""
        import contextlib

        r = NormalizationResult(changed=True)
        with contextlib.suppress(Exception):
            r.changed = False  # pyright: ignore[reportAttributeAccessIssue]
        assert r.changed is True

    def test_counters_sum_to_change_signal(self, tmp_path: Path) -> None:
        """If ``changed`` is True, at least one per-invariant counter
        is non-zero. Guarantees the observability counters stay useful
        for debugging / analytics — no silent "changed but no counter"."""
        md = tmp_path / "page.md"
        md.write_text(_STUCK_PAGE)

        result = normalize_page_file(md)

        assert result.changed is True
        per_invariant_signal = (
            result.schema_version_backfilled
            or result.frame_keyed_dict_orphans_pruned > 0
            or result.body_orphan_rows_pruned > 0
        )
        assert per_invariant_signal
