"""Tests for the frame-keyed key-set invariant in _build_frontmatter.

Issue: figmaclaw#121 — cross-run enrichment loops.

Invariant under test:

    ∀ d ∈ {enriched_frame_hashes, raw_frames, raw_tokens, frame_sections}:
        keys(d) ⊆ frames

Enforced at the single frontmatter write chokepoint (_build_frontmatter).
Orphan keys cannot survive a round-trip through frontmatter rendering,
regardless of caller correctness.

Rationale: on 2026-04-15, a figmaclaw pull on showcase-v2-11550-42383.md
shrank ``frames`` from 102 → 4 (Figma reorganization), but
``enriched_frame_hashes`` retained ~100 orphan entries. The body still had
~60 table rows for those dead IDs. The enricher then saw 60 unresolved
frames, could not make progress, and burned ~5 min of CI every hour for 24+
hours. The invariant in this file prevents the same failure mode from being
reintroduced via a new frame-keyed dict or a caller that forgets to prune.
"""

from __future__ import annotations

from figmaclaw.figma_frontmatter import (
    FigmaPageFrontmatter,
    FrameComposition,
    RawTokenCounts,
    SectionNode,
)
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import build_page_frontmatter
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection


def _page_with_frames(node_ids: list[str]) -> FigmaPage:
    section = FigmaSection(
        node_id="10:10",
        name="Section",
        frames=[FigmaFrame(node_id=nid, name=f"F-{nid}") for nid in node_ids],
    )
    return FigmaPage(
        file_key="fk",
        file_name="F",
        page_node_id="1:1",
        page_name="Page",
        page_slug="page",
        figma_url="https://figma.com/design/fk?node-id=1-1",
        sections=[section],
        flows=[],
        version="v",
        last_modified="2026-04-18T00:00:00Z",
    )


def _parse(frontmatter_text: str) -> FigmaPageFrontmatter:
    parsed = parse_frontmatter(f"{frontmatter_text}\n\n# body\n")
    assert parsed is not None
    return parsed


def test_enriched_frame_hashes_orphans_pruned_on_write():
    """Orphan keys in enriched_frame_hashes are stripped when writing frontmatter.

    Reproduces figmaclaw#121 at the unit level: a caller passes
    enriched_frame_hashes with keys for frames that no longer exist in the
    page; the rendered frontmatter must contain only the valid keys.
    """
    page = _page_with_frames(["11:1", "11:2"])
    stale_hashes = {"11:1": "a" * 8, "11:2": "b" * 8, "DEAD:1": "c" * 8, "DEAD:2": "d" * 8}

    fm_text = build_page_frontmatter(page, enriched_frame_hashes=stale_hashes)
    parsed = _parse(fm_text)

    assert set(parsed.enriched_frame_hashes.keys()) == {"11:1", "11:2"}
    assert set(parsed.enriched_frame_hashes.keys()) <= set(parsed.frames)


def test_raw_frames_orphans_pruned_on_write():
    page = _page_with_frames(["11:1"])
    stale_raw = {
        "11:1": FrameComposition(raw=3, ds=["ButtonV2"]),
        "DEAD:1": FrameComposition(raw=1, ds=[]),
    }

    fm_text = build_page_frontmatter(page, raw_frames=stale_raw)
    parsed = _parse(fm_text)

    assert set(parsed.raw_frames.keys()) == {"11:1"}
    assert set(parsed.raw_frames.keys()) <= set(parsed.frames)


def test_raw_tokens_orphans_pruned_on_write():
    page = _page_with_frames(["11:1"])
    stale_tokens = {
        "11:1": RawTokenCounts(raw=1, stale=0, valid=0),
        "DEAD:1": RawTokenCounts(raw=2, stale=0, valid=0),
    }

    fm_text = build_page_frontmatter(page, raw_tokens=stale_tokens)
    parsed = _parse(fm_text)

    assert set(parsed.raw_tokens.keys()) == {"11:1"}
    assert set(parsed.raw_tokens.keys()) <= set(parsed.frames)


def test_frame_sections_orphans_pruned_on_write():
    page = _page_with_frames(["11:1"])
    stale_sections = {
        "11:1": [SectionNode(node_id="11:1a", name="c", x=0, y=0, w=10, h=10)],
        "DEAD:1": [SectionNode(node_id="DEAD:1a", name="g", x=0, y=0, w=10, h=10)],
    }

    fm_text = build_page_frontmatter(page, frame_sections=stale_sections)
    parsed = _parse(fm_text)

    assert set(parsed.frame_sections.keys()) == {"11:1"}
    assert set(parsed.frame_sections.keys()) <= set(parsed.frames)


def test_all_frame_keyed_dicts_pruned_in_one_write():
    """All four frame-keyed dicts are pruned in a single call.

    This pins the "every dict gets pruned every time" contract — if a future
    contributor adds a fifth frame-keyed dict, they must either update this
    test or pass this test by construction (i.e. by extending the chokepoint).
    """
    page = _page_with_frames(["11:1", "11:2"])
    bad_keys = {"DEAD:1", "DEAD:2"}
    ok_keys = {"11:1", "11:2"}

    fm_text = build_page_frontmatter(
        page,
        enriched_frame_hashes={**dict.fromkeys(ok_keys, "a"), **dict.fromkeys(bad_keys, "b")},
        raw_frames={
            **{k: FrameComposition(raw=1) for k in ok_keys},
            **{k: FrameComposition(raw=1) for k in bad_keys},
        },
        raw_tokens={
            **{k: RawTokenCounts(raw=1) for k in ok_keys},
            **{k: RawTokenCounts(raw=1) for k in bad_keys},
        },
        frame_sections={
            **{
                k: [SectionNode(node_id=f"{k}c", name="c", x=0, y=0, w=1, h=1)]
                for k in ok_keys
            },
            **{
                k: [SectionNode(node_id=f"{k}c", name="c", x=0, y=0, w=1, h=1)]
                for k in bad_keys
            },
        },
    )
    parsed = _parse(fm_text)

    frames_set = set(parsed.frames)
    assert set(parsed.enriched_frame_hashes.keys()) <= frames_set
    assert set(parsed.raw_frames.keys()) <= frames_set
    assert set(parsed.raw_tokens.keys()) <= frames_set
    assert set(parsed.frame_sections.keys()) <= frames_set
    assert not (set(parsed.enriched_frame_hashes.keys()) & bad_keys)
    assert not (set(parsed.raw_frames.keys()) & bad_keys)
    assert not (set(parsed.raw_tokens.keys()) & bad_keys)
    assert not (set(parsed.frame_sections.keys()) & bad_keys)


def test_enriched_schema_version_preserved_through_rewrite():
    """enriched_schema_version must never be silently dropped on write.

    See figmaclaw#121: a real pull commit (f09548074) rewrote a file's
    frontmatter and dropped `enriched_schema_version: 1`, which later had to
    be reconstructed as 0 — downgrading the file's enrichment state.
    """
    page = _page_with_frames(["11:1"])
    fm_text = build_page_frontmatter(page, enriched_schema_version=1)
    parsed = _parse(fm_text)
    assert parsed.enriched_schema_version == 1


def test_pruning_is_no_op_when_all_keys_valid():
    """Pruning must not mutate valid input — only orphans are removed."""
    page = _page_with_frames(["11:1", "11:2"])
    clean = {"11:1": "a" * 8, "11:2": "b" * 8}

    fm_text = build_page_frontmatter(page, enriched_frame_hashes=clean)
    parsed = _parse(fm_text)

    assert parsed.enriched_frame_hashes == clean
