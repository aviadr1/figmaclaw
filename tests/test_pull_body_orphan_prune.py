"""Tests for orphan frame-row pruning in _rewrite_frontmatter_preserving_body.

Issue: figmaclaw#121 — cross-run enrichment loops.

Invariant under test:

    After a pull that shrinks ``frames``, the body contains no frame rows
    whose node_id is outside the new ``frames`` list.

This is the structural twin of the key-set invariant (test_frontmatter_
key_set_invariant.py). Both are required: frontmatter alone being consistent
is not enough — the body's frame table also has to drop orphan rows, or the
enricher's ``pending_frame_node_ids`` will keep reporting them as unresolved
forever.

Non-goal: mechanical rewrites of prose, section intros, or page summaries.
Those are and remain the exclusive domain of the LLM enrich pass.
"""

from __future__ import annotations

from pathlib import Path

from figmaclaw.pull_logic import _prune_orphan_frame_rows, _rewrite_frontmatter_preserving_body


BODY_WITH_ORPHAN_ROWS = """
# Web App / Showcase v2

[Open in Figma](https://www.figma.com/design/fk?node-id=1-1)

This page covers the Showcase V2 feature end-to-end.

## AI wireframes (`11579:137`)

Early-stage wireframes.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Kept row | `11:1` | A real description. |
| Orphan row 1 | `DEAD:1` | (no screenshot available) |
| Orphan row 2 | `DEAD:2` | (no screenshot available) |
| Another kept row | `11:2` | Another description. |

## Screen flow

```mermaid
flowchart LR
  A --> B
```
"""


def test_prune_orphan_frame_rows_keeps_allowed_and_drops_orphans():
    cleaned = _prune_orphan_frame_rows(BODY_WITH_ORPHAN_ROWS, {"11:1", "11:2"})

    assert "`11:1`" in cleaned
    assert "`11:2`" in cleaned
    assert "`DEAD:1`" not in cleaned
    assert "`DEAD:2`" not in cleaned


def test_prune_orphan_frame_rows_preserves_prose_and_mermaid():
    cleaned = _prune_orphan_frame_rows(BODY_WITH_ORPHAN_ROWS, {"11:1", "11:2"})

    assert "# Web App / Showcase v2" in cleaned
    assert "This page covers the Showcase V2 feature end-to-end." in cleaned
    assert "## AI wireframes (`11579:137`)" in cleaned
    assert "Early-stage wireframes." in cleaned
    assert "```mermaid" in cleaned
    assert "flowchart LR" in cleaned


def test_prune_orphan_frame_rows_preserves_table_header_and_separator():
    cleaned = _prune_orphan_frame_rows(BODY_WITH_ORPHAN_ROWS, {"11:1", "11:2"})

    assert "| Screen | Node ID | Description |" in cleaned
    assert "|--------|---------|-------------|" in cleaned


def test_prune_orphan_frame_rows_empty_allowed_is_no_op():
    """When the new frames list is empty, the body is left alone.

    Legacy/bare files with no frontmatter shouldn't have their bodies
    structurally mutated as a side-effect of pull.
    """
    cleaned = _prune_orphan_frame_rows(BODY_WITH_ORPHAN_ROWS, set())
    assert cleaned == BODY_WITH_ORPHAN_ROWS


def test_rewrite_frontmatter_preserving_body_end_to_end(tmp_path: Path):
    """Full integration: rewriting a file with shrunken frames drops orphan rows.

    This reproduces the showcase-v2-11550-42383.md incident shape: a page
    with 4 kept frames and a body full of rows for stale node IDs from a
    prior Figma reorganization.
    """
    md = f"""---
file_key: fk
page_node_id: '1:1'
frames: ['11:1', '11:2', 'DEAD:1', 'DEAD:2']
enriched_schema_version: 0
---

{BODY_WITH_ORPHAN_ROWS.strip()}
"""
    path = tmp_path / "page.md"
    path.write_text(md)

    new_fm = (
        "---\n"
        "file_key: fk\n"
        "page_node_id: '1:1'\n"
        "frames: ['11:1', '11:2']\n"
        "enriched_schema_version: 0\n"
        "---"
    )

    _rewrite_frontmatter_preserving_body(path, md, new_fm)
    written = path.read_text()

    assert "frames: ['11:1', '11:2']" in written
    assert "`11:1`" in written
    assert "`11:2`" in written
    assert "`DEAD:1`" not in written
    assert "`DEAD:2`" not in written
    assert "## AI wireframes (`11579:137`)" in written
    assert "```mermaid" in written


def test_rewrite_frontmatter_preserving_body_no_shrink_no_body_change(tmp_path: Path):
    """If frames don't shrink, body is byte-for-byte preserved.

    This is the contract for the happy path — pull that doesn't remove any
    frames must not touch the body at all, even structurally.
    """
    md = f"""---
file_key: fk
page_node_id: '1:1'
frames: ['11:1', '11:2']
enriched_schema_version: 0
---

{BODY_WITH_ORPHAN_ROWS.strip()}
"""
    path = tmp_path / "page.md"
    path.write_text(md)

    new_fm = (
        "---\n"
        "file_key: fk\n"
        "page_node_id: '1:1'\n"
        "frames: ['11:1', '11:2', 'DEAD:1', 'DEAD:2']\n"
        "enriched_schema_version: 0\n"
        "---"
    )

    _rewrite_frontmatter_preserving_body(path, md, new_fm)
    written = path.read_text()

    _, _, body_after = written.partition("---\n")
    _, _, body_after = body_after.partition("---\n")
    _, _, body_before = md.partition("---\n")
    _, _, body_before = body_before.partition("---\n")
    assert body_after == body_before
