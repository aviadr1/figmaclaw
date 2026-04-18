"""In-place structural normalization for figmaclaw page files (figmaclaw#123).

Every selection/entry boundary in figmaclaw that READS a page file must
call :func:`normalize_page_file` as its first step. The function is
idempotent — calling it twice makes zero changes on the second call —
and narrowly structural: it never touches prose, section intros,
Mermaid charts, or any body content other than canonical frame rows
whose node_id is objectively invalid (not in ``frames``).

Why this exists
---------------
figmaclaw invariants (see CLAUDE.md anti-loop policy) are enforced at
WRITE time: the ``_build_frontmatter`` chokepoint prunes frame-keyed
dicts, ``_rewrite_frontmatter_preserving_body`` drops orphan body rows.
But write time is not the only time a file can enter a bad state:

- A file written by an older figmaclaw version predates a newer invariant.
- A merge conflict resolution can land inconsistent state on disk.
- A hand edit can violate shape rules.
- A pull pass that's filtered out by ``--since`` never touches a file,
  so its write-time invariants never fire on that file.

Meanwhile, multiple code paths READ these files without writing:
``claude-run`` walks the filesystem and dispatches enrichment, ``inspect``
renders a summary, etc. If only writers heal, readers encounter stuck
state repeatedly.

The fix is an explicit, shared healing entry point that every reader
calls. This is that entry point.

DRY by construction
-------------------
This module introduces NO new parsing, NO new invariant logic, NO new
write path. It composes three existing canonical helpers:

- :func:`figma_render.rebuild_frontmatter_from_parsed` — rebuilds the
  YAML frontmatter through the ``_build_frontmatter`` chokepoint, which
  already prunes frame-keyed dicts to ``⊆ frames`` and serializes
  ``unresolvable_frames`` validated by the parse-time validator.
- :func:`body_validation.iter_body_frame_rows` (via
  ``pull_logic._prune_orphan_frame_rows``) — the canonical fence-aware
  frame-row walker; orphan rows are dropped, prose untouched.
- ``_backfill_schema_version`` (below) — moved here from
  ``claude_run._migrate_missing_enrichment_schema_version``; it's the
  same operation, just called from a single canonical location now.

If you find yourself tempted to add a fourth helper, ask whether it
belongs in one of the three places above instead — keep this module
a composition, not a reimplementation.
"""

from __future__ import annotations

from pathlib import Path

import pydantic

from figmaclaw.figma_parse import parse_frontmatter, split_frontmatter


class NormalizationResult(pydantic.BaseModel):
    """Observable outcome of a :func:`normalize_page_file` call.

    Frozen pydantic model (per CLAUDE.md conventions) so callers can
    rely on it being hashable/comparable in tests and assertions.

    ``changed`` is the primary bit: False means the file was already
    in a normalized state (or couldn't be normalized — check ``reason``).
    True means bytes on disk changed.

    The per-invariant counters are for observability. They are exact,
    not estimates: each one counts a specific structural operation that
    was applied. Downstream summarization code should sum them for a
    top-line "structural actions applied" metric rather than computing
    the numbers by diffing text.
    """

    model_config = pydantic.ConfigDict(frozen=True)

    changed: bool = False
    reason: str = ""
    schema_version_backfilled: bool = False
    frame_keyed_dict_orphans_pruned: int = 0
    body_orphan_rows_pruned: int = 0


def _backfill_schema_version(text: str) -> tuple[str, bool]:
    """Ensure ``enriched_schema_version`` field is present in frontmatter.

    Migrated from :func:`claude_run._migrate_missing_enrichment_schema_version`;
    behavior is byte-for-byte identical. Returns ``(possibly-migrated-text,
    was_migrated)``.

    Legacy figmaclaw pages lacked an explicit ``enriched_schema_version``.
    The selector/inspect parity contract (figmaclaw#111) requires the field
    to always be present; missing is treated as ``0`` (= ENRICH MUST).
    """
    parts = split_frontmatter(text)
    if parts is None:
        return text, False
    fm_block, body = parts
    if "enriched_schema_version:" in fm_block:
        return text, False
    if "file_key:" not in fm_block:
        return text, False
    new_fm_block = f"{fm_block.rstrip()}\nenriched_schema_version: 0"
    return f"---\n{new_fm_block}\n---\n{body}", True


def _count_frame_keyed_orphans(fm: object, allowed: set[str]) -> int:
    """Count entries in frame-keyed dicts whose key is not in *allowed*.

    Uses :attr:`figma_frontmatter.FigmaPageFrontmatter` attributes
    directly rather than hard-coding the field names, so adding a new
    frame-keyed dict in the model only requires registering it in
    ``_FRAME_KEYED_DICT_FIELDS`` below. Kept in sync with the chokepoint
    prune in :func:`figma_render._build_frontmatter`.
    """
    total = 0
    for field_name in _FRAME_KEYED_DICT_FIELDS:
        d = getattr(fm, field_name, None) or {}
        total += sum(1 for k in d if k not in allowed)
    return total


# Single source of truth for which frontmatter fields are frame-keyed
# dicts. Kept in lockstep with the prune call-list in
# ``figma_render._build_frontmatter`` via a guardrail test
# (``test_frame_keyed_dict_fields_match_chokepoint``).
_FRAME_KEYED_DICT_FIELDS: tuple[str, ...] = (
    "enriched_frame_hashes",
    "raw_frames",
    "raw_tokens",
    "frame_sections",
    "unresolvable_frames",
)


def normalize_page_file(md_path: Path) -> NormalizationResult:
    """Apply every structural invariant to *md_path* in-place.

    Invariants applied, in order:

    1. ``enriched_schema_version`` field exists in frontmatter
       (backfilled to 0 if missing) — selector/inspect parity.
    2. Frame-keyed dict keys ⊆ ``frames`` — pruned through the
       ``_build_frontmatter`` chokepoint (enriched_frame_hashes,
       raw_frames, raw_tokens, frame_sections, unresolvable_frames).
    3. Canonical body frame rows with node_id ∉ ``frames`` are dropped.
    4. ``unresolvable_frames`` shape and size — already enforced by the
       pydantic validator on parse, so simply parsing-then-rewriting
       normalizes any legacy on-disk shape that passed the validator.

    Idempotent: a normalized file passed through this function is
    byte-for-byte unchanged and returns ``NormalizationResult(changed=False)``.

    Body prose is preserved verbatim. This function is the one narrow
    exception to "code never rewrites body," and only within the scope
    the body-preservation invariants (BP-1 through BP-6) already permit:
    structural removal of canonical rows that point to frames not in
    the authoritative ``frames`` list.

    Safe to call on non-figmaclaw files — returns ``changed=False,
    reason='no frontmatter'`` when the file isn't a figmaclaw page.
    """
    from figmaclaw.figma_render import rebuild_frontmatter_from_parsed
    from figmaclaw.pull_logic import _prune_orphan_frame_rows

    try:
        original = md_path.read_text()
    except OSError as exc:
        return NormalizationResult(reason=f"read failed: {exc}")

    # Invariant 1: backfill schema_version before parsing so pydantic
    # validators see a canonical frontmatter.
    after_backfill, did_backfill = _backfill_schema_version(original)

    # Parse. The pydantic model runs its validators here, which already
    # enforce the ``unresolvable_frames`` shape + ⊆ frames invariants on
    # the parsed model (see figmaclaw#121 security review).
    try:
        fm = parse_frontmatter(after_backfill)
    except Exception as exc:
        return NormalizationResult(reason=f"parse failed: {exc}")
    if fm is None:
        return NormalizationResult(reason="no frontmatter")

    parts = split_frontmatter(after_backfill)
    if parts is None:
        return NormalizationResult(reason="split failed")
    _, body = parts

    allowed = set(fm.frames)

    # Invariant 2: frame-keyed dict key-set. Re-rendering through the
    # chokepoint applies the prune. Counting orphans BEFORE the rebuild
    # (not via diff after) keeps the counter exact.
    orphan_dict_count = _count_frame_keyed_orphans(fm, allowed)
    new_fm = rebuild_frontmatter_from_parsed(fm)

    # Invariant 3: body orphan frame rows. Use the canonical walker.
    body_lines_before = body.count("\n")
    new_body = _prune_orphan_frame_rows(body, allowed) if allowed else body
    body_lines_after = new_body.count("\n")
    orphan_rows_count = body_lines_before - body_lines_after

    new_text = f"{new_fm}\n{new_body}"
    if new_text == original:
        return NormalizationResult(
            changed=False,
            schema_version_backfilled=did_backfill,
        )

    md_path.write_text(new_text)
    return NormalizationResult(
        changed=True,
        schema_version_backfilled=did_backfill,
        frame_keyed_dict_orphans_pruned=orphan_dict_count,
        body_orphan_rows_pruned=orphan_rows_count,
    )
