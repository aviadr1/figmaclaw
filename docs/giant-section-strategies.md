# Giant Section Enrichment Strategies

> **Status:** Analysis complete (2026-04-03). Strategy 2 (frame-level updates) recommended.
> This document captures the data, debate, and decision for handling large sections
> (>60 frames) during enrichment.
>
> **Canon cross-reference:** the section-level enrichment design that emerged from this analysis is canonized as [D10 in `figmaclaw-canon.md` §5](figmaclaw-canon.md#d10-section-level-enrichment-via-per-frame-hash-aggregation). This doc is the historical analysis behind D10.

## The problem

26 sections across pending pages have >100 frames. The largest is 330 frames.
Current section-mode processes all frames in one section in a single Claude
invocation. For 265 frames, that's 34 subagent batches (~39 turns) with
max_turns=80. The write-body output is ~13K tokens — risk of truncation.

**Almost all giant sections are "(Ungrouped)"** — frames that designers didn't
organize into Figma sections.

## Data (2026-04-03)

### Giant section distribution (across all pending pages)

| Frames | Count | Examples |
|--------|-------|---------|
| 200-330 | 5 | community-ui (330), community-wireframes (265), live-ui-exploration-v2 (265) |
| 100-200 | 21 | research-inspo (185), page-1-0-23931 (155), live-new (149) |
| 60-100 | 9 | reach-playground (92), live-ui-exploration (89), poc-video (80) |

All but 2 (icons "Variants" and one "Section 1") are "(Ungrouped)" sections.

### The two giant pages

**page-1-0-1.md (528 frames, 28 sections):**
- Largest section: (Ungrouped) 115 frames
- 2 sections >40 frames, 18 sections ≤20 frames
- Estimated section-mode time: 37 min (fits in 55 min)

**community-wireframes-7-15127.md (596 frames, 53 sections):**
- Largest section: (Ungrouped) 265 frames
- 1 section >40 frames, 49 sections ≤20 frames
- Estimated section-mode time: 64 min (DOES NOT FIT in 55 min)
- The 265-frame Ungrouped section alone needs ~39 turns

### Observed section-mode performance

| Page | Frames | Sections | Section time | Finalize | Total |
|------|--------|----------|-------------|----------|-------|
| mobile-1-9 | 82 | 4 | 6.3 min | 5.7 min | 12.0 min |
| round-2 | 90 | 7 | 9.1 min | 3.4 min | 12.5 min |
| playground-desktop | 83 | 10 | 10.3 min | 3.3 min | 13.7 min |
| explorations-leadership | 87 | 11 | 11.4 min | 6.5 min | 17.9 min |
| invite-dialog | 88 | 10 | 10.0 min | 14.2 min | 24.1 min |

Per-section: ~65s median (includes Claude startup, screenshots, describe, write-section, commit).

## Strategy analysis

### Strategy 1: Chunk large sections

Split sections >N frames into chunks. Each chunk = one Claude invocation that
processes frames N+1 through N+M, merging new descriptions into the existing section.

**Implementation:** `claude-run` detects large sections, splits into chunks.
Each chunk's prompt says "describe only frames X through Y, preserve existing
descriptions for other frames." `write-body --section` replaces the whole section,
so each chunk must produce the COMPLETE section table with existing + new descriptions.

**Verdict: Fragile.** Claude must read the existing section, copy all non-target
descriptions verbatim, and insert new ones. LLMs are unreliable at verbatim copying
of large content. A 265-frame table has ~265 rows — Claude might drop rows,
reorder them, or subtly change existing descriptions.

### Strategy 2: Frame-level description updates ⭐ RECOMMENDED

New command: `figmaclaw write-descriptions <file> --descriptions '{"node_id": "desc", ...}'`

Finds each frame's row in the markdown table by matching `` `node_id` `` in the
second column, and replaces the description cell. Purely mechanical — no LLM
needed for the write step.

**Enrichment flow for large sections:**
1. `screenshots --section <id> --pending` → download pending frames
2. Claude reads 8 screenshots, returns JSON: `{"node_id": "description", ...}`
3. `write-descriptions <file> --descriptions <json>` → updates those 8 rows
4. `git commit + push`
5. Repeat for next batch of 8. Resumable — each batch is committed.

**Why this is best:**
- **True incremental:** each batch of 8 frames = 1 commit. Timeout loses ≤8 descriptions.
- **No LLM for the write step:** mechanical row replacement. No risk of truncation, dropped rows, or rewriting.
- **Composable:** works within section-mode (for large sections) or standalone.
- **Simple implementation:** regex match on table rows by node_id, replace 3rd column.
- **Section intro written separately:** one more Claude call after all frames described.

**Implementation complexity:** Low. It's a regex find-and-replace on markdown table rows.
The row format is fixed: `| {name} | \`{node_id}\` | {description} |`.

### Strategy 3: Auto-split Ungrouped

During `figmaclaw pull`, split (Ungrouped) sections with >N frames into
"(Ungrouped 1)", "(Ungrouped 2)", etc.

**Verdict: Bad.** Breaks the Figma→markdown mapping contract. Creates synthetic
sections with fake node_ids. When frames are added/removed, the split boundaries
shift and previously-enriched descriptions end up in wrong sub-sections. Ongoing
maintenance nightmare.

### Strategy 4: Parallel section processing

Process 3-5 sections simultaneously in parallel Claude invocations.

**Verdict: Doesn't solve the problem.** The bottleneck isn't total section count —
it's individual section SIZE. Processing 5 small sections in parallel saves time,
but the 265-frame Ungrouped section is still one sequential blob. Also, concurrent
git push from multiple processes would conflict.

### Strategy 5: Two-phase (describe then write)

Phase 1: Generate descriptions → JSON sidecar. Phase 2: Write body from JSON.

**Verdict: Overcomplicated Strategy 2.** The JSON sidecar is unnecessary if we
can write descriptions directly to the markdown table (Strategy 2). The only
advantage is decoupling, but `write-descriptions` already achieves that.

## Recommendation

**Strategy 2 (frame-level description updates)** is the clear winner:
- Simplest implementation (regex row replacement)
- Most incremental (8 frames per commit)
- Most resilient (timeout loses 8 descriptions max)
- No LLM needed for the write step
- Composes with existing section-mode

**Implementation plan:**
1. New command: `figmaclaw write-descriptions <file> --descriptions '{...}'`
2. Update `figma-section-enrich.md` prompt: for large sections (>60 frames),
   use `write-descriptions` instead of `write-body --section`
3. Update `claude-run` orchestration: detect large sections, use frame-level
   prompt instead of section-level prompt
4. Section intro handled separately after all frames described

**Implemented (2026-04-03):** One threshold everywhere: 80 frames.
- Pages ≤80 frames: whole-page (`write-body`)
- Sections ≤80 frames: section-mode (`write-body --section`)
- Sections >80 frames: frame-level (`write-descriptions`, chunked at 80)

**Empirical maximums (needs updating as data comes in):**
- Whole-page proven up to 25 frames (80 threshold is extrapolation)
- Section-mode proven up to 90 frames total (28 frames per section max)
- Frame-level: not yet tested in CI
