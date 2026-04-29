"""Validation tests for unresolvable_frames (security review of figmaclaw#121).

The pydantic model rejects tombstones whose shape could indicate
corruption, hand-edit mistakes, or malicious injection:

- keys must match Figma node_id format (``<number>:<number>``)
- values must be short lowercase hex (content hash shape)
- tombstones for node_ids not in ``frames`` are pruned on parse

Strict parse-time validation is the belt-and-braces pair to the
chokepoint prune in ``_build_frontmatter`` — no orphan or malformed
tombstone can surface to downstream code, regardless of how the bad
value got onto disk (hand edit, bad merge, tampering).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from figmaclaw.figma_frontmatter import FigmaPageFrontmatter


def _base_kwargs(**overrides):
    return {
        "file_key": "fk",
        "page_node_id": "1:1",
        "frames": ["11:1", "11:2"],
        **overrides,
    }


class TestNodeIdShape:
    def test_rejects_non_numeric_node_id(self) -> None:
        with pytest.raises(ValidationError, match="not a valid Figma"):
            FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={"evil-key": "abc123"}))

    def test_rejects_node_id_missing_colon(self) -> None:
        with pytest.raises(ValidationError, match="not a valid Figma"):
            FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={"1234": "abc123"}))

    def test_accepts_valid_figma_node_id(self) -> None:
        fm = FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={"11:1": "abc123"}))
        assert fm.unresolvable_frames == {"11:1": "abc123"}


class TestHashShape:
    def test_rejects_non_hex_hash(self) -> None:
        with pytest.raises(ValidationError, match="not lowercase hex"):
            FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={"11:1": "NOT-A-HASH"}))

    def test_rejects_uppercase_hex(self) -> None:
        """Standardize on lowercase hex — matches how frame_hash is written."""
        with pytest.raises(ValidationError, match="not lowercase hex"):
            FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={"11:1": "ABCDEF"}))

    def test_rejects_empty_hash(self) -> None:
        with pytest.raises(ValidationError, match="length 0"):
            FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={"11:1": ""}))

    def test_rejects_oversized_hash(self) -> None:
        """Cap prevents frontmatter bloat from malicious/corrupt input."""
        with pytest.raises(ValidationError, match="out of range"):
            FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={"11:1": "a" * 100}))


class TestOrphanPruneOnParse:
    def test_orphan_tombstones_are_pruned_on_parse(self) -> None:
        """A tombstone for a node_id not in `frames` is dropped on parse.

        Defense-in-depth for the `_build_frontmatter` chokepoint: even
        if a bad commit lands, downstream code never sees the orphan.
        """
        fm = FigmaPageFrontmatter(
            **_base_kwargs(
                # DEAD:1 fails the node_id shape AND is not in frames — the
                # shape validator would reject it first. Use a key that
                # passes shape but isn't in frames so we exercise the
                # ⊆-frames prune specifically.
                unresolvable_frames={"11:1": "abc123", "99:99": "def456"},
            )
        )
        assert fm.unresolvable_frames == {"11:1": "abc123"}
        assert "99:99" not in fm.unresolvable_frames

    def test_empty_tombstones_with_non_empty_frames_is_fine(self) -> None:
        fm = FigmaPageFrontmatter(**_base_kwargs(unresolvable_frames={}))
        assert fm.unresolvable_frames == {}


class TestFenceAwareWalker:
    """Pins the fence-awareness of figma_md_parse's canonical walker.

    Design review of figmaclaw#121: the CLAUDE.md policy claims a single
    canonical body walker, but figma_md_parse._collect_frames_in_range
    previously did not track fences. A fenced code block containing a
    frame-row-shaped line would be walked as if real. Now both walkers
    (this one and body_validation.iter_body_frame_rows) agree.
    """

    def test_collect_frames_skips_rows_inside_fence(self) -> None:
        from figmaclaw.figma_md_parse import section_line_ranges

        md = """## Section (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Real | `11:1` | desc |

```
| Fake | `DEAD:1` | in a fence |
```

| Screen | Node ID | Description |
|--------|---------|-------------|
| Real | `11:2` | desc |
"""
        ranges = section_line_ranges(md)
        assert len(ranges) == 1
        section, _, _ = ranges[0]
        node_ids = {f.node_id for f in section.frames}
        assert node_ids == {"11:1", "11:2"}
        assert "DEAD:1" not in node_ids
