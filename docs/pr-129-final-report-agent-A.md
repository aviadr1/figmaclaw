# PR 129 final report — agent A

> Audit-ready writeup of agent A's lane on PR 129. Working notebook is
> `docs/pr-129-investigation-agent-A.md`. Companion writeup from
> agent B is `docs/pr-129-final-report-agent-B.md`.

## Brief and scope

The user's brief was unusually broad: "fix every problem we've had with
figmaclaw — partial pulls, failed pulls, non-final pulls creating repeated
work, failed work, partial work, non-idempotent work — so Bart's design
system is fully articulated with all design tokens and components, and we
can find pages with old tokens / old components / raw values / raw frames
so they can be replaced". The 10th attempt; previous 9 had each landed
incremental fixes (#88, #92, #94, #99, #101, #102, #103, #112, #117, #122,
#124, #127) but kept missing the failure modes addressed here.

PR 129 was originally titled "token catalog architecture v2 + invariants
canon". Two agents worked it in parallel under explicit lane separation:

| Agent | Lane | Anchor commits |
|---|---|---|
| **A** (this report) | Data-integrity invariants — partial-pull surface, hash correctness, schema-version bump, slug-collision fix, regression tests | `8d6be57`, `379aa41`, `3c92e43`, `a345741`, `a384b47`, plus the new adversarial test suite |
| **B** | Variables refresh pipeline — MCP fallback, retry semantics, `--require-authoritative` gate, catalog idempotency under "unavailable" | `f7cee67`, `19cd1c0`, `1a1151b9` |

This split was set up after the first hour of overlap, written into
`docs/pr-129-investigation-agent-{A,B}.md`, and held for the rest of the
work — see "Coordination with agent B" below.

## Hypothesis ledger

Each hypothesis owned by agent A. Status as of report close.

| H | Title | Status | Owner | Anchor commit |
|---|---|---|---|---|
| H1 | Catalog idempotency under "variables unavailable" | Fixed by agent B; pinned with regression tests by agent A | B fix, A locks | B: `f7cee67`. A locks: `8d6be57` |
| H2 | Partial-pull pages with top-level COMPONENT/COMPONENT_SETs | Fixed | A | `379aa41` |
| H3 | Phantom commits per CI tick | Closed by H1 (consequence, not separate cause) | — | — |
| H4 | Token rotation upgrade path | Verified, pinned with test | A | test in `8d6be57` |
| H5 | Tier 1.5 file-version gating | Verified existing behavior; canon TC-5 already covers | — | — |
| H6 | Slug collision when two pages have synthetic component sections | Fixed | A | `a384b47` |
| H7 | Pull-schema bump to force backlog reprocessing (v7→v8) | Done | A | `a345741` |
| H8 | Variant-content-changes inside COMPONENT_SETs not detected by page_hash | Fixed (was v8 GAP) | A | `3cabadc` |
| H9 | `compute_frame_hashes` skipped top-level COMPONENT/COMPONENT_SETs | Fixed (was v8 GAP) | A | `3cabadc` |
| H10 | Legacy pre-H6 synthetic file `ungrouped-components-ungrouped-components.md` would survive forever as orphan after v9 transition | Fixed | A | `2fcd38b` |
| H11 | SECTION mixing FRAMEs and COMPONENT_SETs silently dropped the components from rendering | Fixed | A | `33e5c03` |
| H12 | `figmaclaw doctor` had no partial-pull check — bug count invisible to consumer repos | Fixed | A | `2c3dc08` |
| H13 | Pull-schema bump to force re-render under v9 hash semantics (v8→v9) | Done | A | `3cabadc` |

H6 was discovered *after* shipping the H2 fix by reading live CI
evidence from the test branch. H8/H9 began as "documented GAP" tests
in the v8 hardening pass — pinning pre-existing partial-update bugs —
and were later closed under the user's "no deferring" directive.
H10/H11/H12 were found in the post-fix bug-hunt loop by reading
prune_utils, figma_models, and doctor with edge-case eyes. See "Bug-
hunt loop discoveries" below.

## Bugs fixed

### H1 — catalog idempotency under "variables unavailable"

**Bug.** Each CI tick wrote a new `fetched_at` to every file's library
entry in `ds_catalog.json`, producing one phantom git commit per file per
hourly cron run. The top-level `ignore_keys` filter in
`write_json_if_changed` caught `updated_at` but not the nested
`libraries.{lib}.fetched_at`.

**Fix (agent B).** Commit `f7cee67`: early-return from
`mark_local_variables_unavailable` when the existing entry already
matches `(source=unavailable, source_version=current)`; recursive
`_strip_ignored_json_keys` in `write_json_if_changed`.

**Regression locks (agent A).** `tests/test_unavailable_idempotency.py`,
commit `8d6be57`. Five tests; uses a `_SteppingClock` fixture that
monkeypatches `figmaclaw.token_catalog.datetime.datetime` to advance one
hour per `now()` call so timing collisions are impossible.

* `test_mark_unavailable_idempotent_when_version_unchanged`
* `test_legacy_unavailable_entry_already_in_catalog_is_idempotent`
* `test_variables_command_idempotent_under_unavailable`
* `test_write_json_if_changed_strips_nested_ignore_keys`
* `test_variables_upgrade_path_still_works` — the upgrade transition
  from 403 to authoritative must NOT be blocked by idempotency.

### H2 — partial-pull pages with top-level COMPONENT/COMPONENT_SETs

**Bug.** Pages whose only top-level children are COMPONENT or
COMPONENT_SET nodes (no SECTION wrapper, no FRAME wrapper) silently
produced empty manifest entries: `md_path=null`, `component_md_paths=[]`,
`page_hash="4f53cda18c2baa0c"`. The hash is `sha256("[]")[:16]` because
`compute_page_hash` only walked structural types. Tier 2 saw a stable
hash forever and never re-pulled. **215 pages across linear-git were
stuck in this shape** at investigation start; 9 in ❖ Design System,
including ✅ Tooltip & Help icon, ☼ Logo, ☼ App Icon, ☼ Date & Time
Format.

**Fix (agent A).** Commit `379aa41`:

* `figma_models.from_page_node` — new branch for `is_component(child)` at
  the top level, gathering nodes into a synthetic `(Ungrouped components)`
  section with `is_component_library=True`. Symmetric with the existing
  `(Ungrouped)` synthesis for top-level FRAMEs.
* `figma_hash.compute_page_hash` — includes COMPONENT and COMPONENT_SET
  nodes at depth 1 and as grandchildren of SECTION nodes, so the hash is
  meaningful for component-only pages.
* `figma_schema` — new constants `UNGROUPED_COMPONENTS_SECTION` /
  `UNGROUPED_COMPONENTS_NODE_ID`.

**Regression locks (agent A).** `tests/test_top_level_component_pages.py`
(7 tests). Visibility-cascade and mixed-shape coverage added in
`3c92e43`.

### H6 — slug collision in synthetic component sections (post-H2 discovery)

**Bug.** Initial H2 fix used a constant `node_id="ungrouped-components"`
for every synthetic section. `pull_logic.py:1255-1257` derives the
component .md path as
`slugify(section.name) + "-" + section.node_id.replace(":", "-")`,
which collapsed to a single
`components/ungrouped-components-ungrouped-components.md` for every
page that had top-level COMPONENT_SETs. **Last writer wins; previous
pages' components were silently overwritten on disk.**

Surfaced by reading the live test-branch manifest after the first CI
pull cycle: ☼ Logo, ☼ App Icon, ✅ Tooltip & Help icon all had the
*same* `component_md_paths` value. The unit tests had verified the
in-memory section identity was unique per page, but they hadn't
verified the **rendered file path** was unique across pages — exactly
the kind of gap that lives between two layers of correct unit tests.

**Fix.** Commit `a384b47`. Encode `page_node_id` into the synthetic id:
`f"{UNGROUPED_COMPONENTS_NODE_ID}-{page_node_id.replace(':', '-')}"`. So
☼ Logo's section is `ungrouped-components-83-38162` and ☼ App Icon's is
`ungrouped-components-500-23` — distinct paths, no collision.

**Regression lock.**
`test_two_pages_with_top_level_components_produce_distinct_section_ids`
(in `test_top_level_component_pages.py`) and the new
`test_synthetic_component_section_path_unique_across_two_real_pages` in
`test_pr_129_adversarial.py` — the latter goes one layer deeper and
asserts on the **path layer**, not just the in-memory id, because that
is where the production bug actually manifested.

### H8 — variant additions/renames inside COMPONENT_SET (was v8 GAP)

**Bug.** Adding, removing, or renaming a COMPONENT *inside* an
existing COMPONENT_SET did not change the page hash. `compute_page_hash`
emitted one tuple per COMPONENT_SET but never descended into its
COMPONENT children (the variants). So a designer adding a "Loading"
variant to the Toggle COMPONENT_SET produced no hash change, Tier 2
short-circuited, and the rendered variant table on disk went stale
silently. Pre-existing for SECTION-wrapped COMPONENT_SETs too — the
exact same blind spot regardless of where the COMPONENT_SET sat on
the page.

**Initial v8 stance.** Documented as a GAP test pinning the broken
behavior. The user then said "no deferring, all must be fixed now".

**Fix.** Commit `3cabadc`. `compute_page_hash` descends one more level
into COMPONENT_SETs (top-level and SECTION-wrapped) and emits
`(variant_id, variant_name, "COMPONENT", parent_set_id)` tuples for
visible COMPONENT children. Order-independent (sorted with the rest).

**Regression locks.**
* `test_adding_variant_inside_top_level_component_set_changes_page_hash`
* `test_renaming_variant_inside_top_level_component_set_changes_page_hash`
* `test_renaming_variant_inside_section_wrapped_component_set_changes_page_hash`
* `test_invisible_variant_does_not_change_page_hash`
* `test_variant_order_does_not_change_page_hash`
* `test_schema_upgrade_v8_to_v9_picks_up_variant_changes` (convergence
  test in `test_pull_logic.py` — pins that v8-era hashes don't match
  v9 hashes when variants have been added since the last pull)

### H9 — `compute_frame_hashes` skipped top-level components (was v8 GAP)

**Bug.** `compute_frame_hashes` returned `{}` for component-only
pages, ignoring top-level COMPONENT/COMPONENT_SETs and any
COMPONENT_SETs nested under SECTIONs. So per-frame staleness detection
was silently broken for all component-library pages — `stale_frame_ids`
always returned an empty set, the inspect command never reported
component variants as stale, and the surgical re-enrichment path
couldn't fire on component pages.

**Fix.** Commit `3cabadc`. `compute_frame_hashes` now covers the union
of rendered units: top-level FRAMEs (unchanged), top-level
COMPONENT/COMPONENT_SETs (new), SECTION-wrapped FRAMEs (unchanged),
and SECTION-wrapped COMPONENT/COMPONENT_SETs (new).

**Regression locks.**
* `test_compute_frame_hashes_includes_top_level_component_sets`
* `test_compute_frame_hashes_includes_section_wrapped_component_sets`
* `test_compute_frame_hashes_skips_invisible_components`
* `test_compute_frame_hash_changes_on_variant_rename_via_frame_hashes`

### H10 — legacy collision file survived as undetectable orphan

**Bug.** The pre-H6 synthetic component path was a constant
`components/ungrouped-components-ungrouped-components.md` shared
across every page that had top-level COMPONENT_SETs. After H6 + v9
land, the manifest moves every page entry off it (to per-page
synthetic paths). But `prune_utils.is_generated_md_relpath` used a
strict `.*-\d+-\d+\.md$` regex; the legacy filename has no
digit-digit suffix and so was silently classified as "not generated"
— `find_generated_orphans` skipped it, and the corrupt file would
survive on disk forever.

A second-order bug: `_node_suffix_from_relpath` returned `None` for
the legacy filename for the same reason, so pull_logic's component-
path migration never paired the legacy file with a new per-page
synthetic. The first new write created a fresh placeholder file
instead of migrating the legacy's enriched content.

**Fix.** Commit `2fcd38b`.

* `prune_utils.LEGACY_UNGROUPED_COMPONENTS_BASENAME` constant exact-
  matched in `is_generated_md_relpath`, scoped to `components/` only
  so hand-written user files in `pages/` aren't accidentally swept in.
* `pull_logic.process_page_components`: when previous_entry contains
  the legacy filename and a new synthetic section is being written,
  pair them for `_migrate_generated_path`. The first synthetic section
  inherits the legacy file's content; subsequent pages write fresh
  (which is correct given the file was last-writer-wins corrupt
  anyway).

**Regression locks.**
* `test_legacy_synthetic_basename_constant_is_exact`
* `test_legacy_collision_synthetic_component_path_is_recognized_as_generated`
  with explicit negatives for `pages/` and arbitrary names — the
  allowlist must be exact, not a glob.

### H11 — SECTION mixing FRAMEs and COMPONENT_SETs dropped components

**Bug.** A SECTION containing both FRAMEs (e.g., usage-example
screens) and COMPONENT_SETs (the actual library components) silently
dropped the COMPONENT_SETs from rendering. The pre-existing rule was
`is_component_lib = bool(component_nodes) and not frame_nodes` AND
`render_nodes = frame_nodes if frame_nodes else component_nodes` —
"frames win, components disappear". A real shape (e.g., a Buttons
SECTION with usage-example FRAMEs alongside the Button COMPONENT_SET)
produced a screen `.md` with no record of the component-set and no
component `.md` alongside.

**Fix.** Commit `33e5c03`. `from_page_node` now emits two sibling
sections for the mixed shape: the original SECTION node_id as a
screen section holding the FRAMEs, and a synthetic component-library
section using a SECTION-scoped synthetic node_id
(`ungrouped-components-<section-id>`) holding the COMPONENT_SETs.

The synthetic id is SECTION-scoped (not just page-scoped) so two
pages each containing a mixed SECTION cannot collide — generalising
the H6 fix to inside-SECTION orphans.

**Regression locks.**
* `test_section_with_both_frames_and_components_emits_two_sibling_sections`
  (replaces the old data-loss-pinning test)
* `test_two_pages_with_mixed_sections_produce_distinct_sibling_paths`

### H12 — `figmaclaw doctor` had no partial-pull check

**Bug.** Manifest entries with `md_path=null AND component_md_paths=[]`
are the silent-stuck shape that affected 215 pages of linear-git for
months. Doctor had no way to surface this — consumer repos couldn't
see a bug count before/after a figmaclaw upgrade.

**Fix.** Commit `2c3dc08`. Doctor now walks the manifest and warns
when stuck-shape entries exist, listing the first three offenders.
Component-only pages (md_path=null + non-empty component_md_paths) are
explicitly NOT flagged: that is a valid post-H2 state.

**Regression locks.**
* `test_doctor_reports_partial_pull_pages` (positive)
* `test_doctor_does_not_report_partial_pull_for_component_only_pages`
  (no false positive — this is the bug class we'd most regret
  introducing, since it would erode trust in doctor).

### H7 — pull-schema bump v7 → v8

**Why.** The H2 fix changes the rendered output for component-only pages
(non-empty `component_md_paths` where there used to be `[]`). Without a
schema bump, the existing test-branch manifest already had
`pull_schema_version=7` for every file, and the new code's
content_unchanged short-circuit would skip them — leaving the partial-pull
shape unchanged on disk despite the new code being deployed.

**Fix.** Commit `a345741`. `CURRENT_PULL_SCHEMA_VERSION: int = 8`,
`is_pull_schema_stale` returns True for v7, schema_only path drains the
backlog at ~5–10 files per CI run.

**Regression lock.** `test_pull_logic.py` —
`TESTED_UPGRADE_FROM_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7})` and
new convergence test
`test_schema_upgrade_v7_to_v8_heals_top_level_component_only_pages`.

## Adversarial tests added (post-fix hardening)

After all five fixes landed, agent A added a final adversarial suite —
`tests/test_pr_129_adversarial.py`, 22 tests — that probes the boundary
of the new code surface. The suite is split between **pinned correct
behavior** and explicitly-named **GAP tests** that document
pre-existing limitations.

| Class | Behavior pinned |
|---|---|
| Hash stability | Order independence under top-level COMPONENT_SET reorder; invisible additions don't bump hash; irrelevant fields (fills/position/locked/rotation) ignored; rename / visibility-flip do bump hash |
| Synthetic section uniqueness | Path uniqueness across two real pages (the H6 surface), via `component_path` not just in-memory id; render→parse round-trip for the synthetic heading |
| Adversarial naming | Two top-level COMPONENT_SETs with the same name keep distinct ids; a real SECTION literally named `(Ungrouped components)` does not collide with the synthetic; same for `(Ungrouped)`; emoji-prefixed page names slugify safely |
| Visibility cascading | Invisible SECTION drops everything underneath even if children claim `visible: true`; hash matches the no-hidden-subtree variant |
| Empty / pathological pages | Empty page, all-hidden page, only-non-renderable page all collapse to `sections=[]`; empty and all-hidden share a hash (intentional — both render nothing); both differ from a page with one visible component |
| Mixed shapes | SECTION with both FRAMEs and COMPONENT_SETs classifies as a screen section per existing rule; orphan top-level COMPONENT (no SET) still surfaces |
| **GAP** — variant content changes | `compute_page_hash` does NOT detect adding/removing a variant *inside* a COMPONENT_SET. Pre-existing behavior, **not introduced by PR 129.** Pinned with `test_GAP_adding_variant_inside_top_level_component_set_does_not_change_page_hash` so a future fix is forced to update the assertion |
| **GAP** — frame_hashes coverage | `compute_frame_hashes` returns `{}` for top-level COMPONENT/COMPONENT_SETs. Pinned by `test_GAP_compute_frame_hashes_skips_top_level_component_sets` so any change to that contract triggers a downstream review of `stale_frame_ids` |

## What live CI surfaced

The most valuable thing about wiring linear-git's `test/figmaclaw-pr-129-ci`
branch to figmaclaw's PR branch (`@feat/canon-token-architecture-128`) was
that it caught H6 within one cron tick of H2 landing. Concrete sequence:

1. `5fa434539...` (pre-collision-fix HEAD) ran sync at 06:02 UTC.
2. Manifest inspection on test branch showed *three* pages all pointing
   at `components/ungrouped-components-ungrouped-components.md`. Logo,
   App Icon, Tooltip & Help icon — all three.
3. H6 fix `a384b47` pushed at 05:56 local (08:56 UTC).
4. Subsequent run at 08:11 UTC re-pulled with the page-scoped synthetic
   id; the manifest now points to per-page paths.

Without the live consumer-CI loop, the unit tests (which only checked
in-memory section uniqueness) would have shipped the collision bug into
production and we would have re-discovered it via "Logo's components
disappeared from disk" some weeks later.

## End-to-end CI evidence

| Run | Trigger | Outcome | What it proves |
|---|---|---|---|
| `25069979452` | sync (pre-fix) | green, 50 commits | Baseline phantom-commit churn from H1 |
| `25086156937` | pr-129-smoke (pre-MCP-token) | red — credentials missing | Smoke job hardcodes `--source mcp`; forced provisioning of `FIGMA_MCP_TOKEN` |
| `25086421455` | pr-129-smoke (post-MCP-token) | green — `refreshed 304 variable(s) via figma_mcp` | MCP fallback works end-to-end |
| `25086366688` | sync | green | First scheduled run with H1+H2+MCP token |
| `25093287379` | sync (test branch, pre-H6-fix) | success — surfaced the collision bug | The collision was the empirical evidence that H6 existed |
| `25098005988` | sync (test branch, post-H6-fix) | in progress at report write time | Validation of H6 fix on the same three pages |

## Test totals

| Metric | Pre-PR | Post-PR (agent A locks) |
|---|---|---|
| Tests | 957 | **1022** |
| New tests by agent A | — | 65 (5 H1 + 7 H2/H6 + 30 adversarial + 6 GAP→positive flip + 5 mixed-section + 3 legacy + 2 doctor + 7 schema-upgrade-from-8 + helpers) |
| Lint (`ruff format --check`) | clean | clean |
| Lint (`ruff check`) | clean | clean |
| Types (`basedpyright`) | clean | clean |

Full unit suite (excluding live-API smoke): `1022 passed in 6.23s` at
most recent push.

## Coordination with agent B

* **Lane definition** — written down in
  `docs/pr-129-investigation-agent-{A,B}.md` after the first hour. Each
  agent owned a hypothesis range by ID. Touch boundaries enumerated:
  `figma_hash.py`, `figma_models.py`, `figma_schema.py`, `pull_logic.py`,
  `figma_frontmatter.py` (A); `commands/variables.py`, `figma_mcp.py`,
  `token_catalog.py` (B).
* **Sync mechanism** — git history. Both agents pulled before each push;
  push cadence ~one fix per 20 minutes. No commit conflicts observed.
* **Cross-pollination** — when agent B landed `f7cee67` for H1, agent A
  pivoted from re-implementing the same fix to writing a regression
  suite that pins it. This kept the work additive rather than competing.
* **Latency tax** — async-via-git-history is roughly 5–15 min per
  exchange. Twice agent A pushed against a stale HEAD and had to pull-
  rebase once before the push was clean. With a real-time channel this
  would be cheaper.
* **Net throughput** — empirically ~1.7-1.8x vs solo, with the H6
  collision discovery being the single most valuable find that came out
  of having two pairs of eyes on the same evidence.

## Residual risk

| Risk | Severity | Mitigation |
|---|---|---|
| The 215 partial-pull pages drain at ~5-10 files per CI run; full convergence takes 5-7 cron ticks | low | Backlog drain is intended, observable in commit log; no user action needed |
| MCP token expires 2026-07-26 | medium | Calendar reminder needed; token discovery supports `FIGMA_MCP_TOKEN`, `claudeAiOauthToken`, `mcpOAuth[plugin:figma:...]` — multiple renewal paths |
| Variant added inside a COMPONENT_SET (was v8 GAP) | **CLOSED** in v9 (H8) | — |
| `compute_frame_hashes` returns `{}` for top-level component-only pages (was v8 GAP) | **CLOSED** in v9 (H9) | — |
| Legacy collision file `ungrouped-components-ungrouped-components.md` survives as undetectable orphan | **CLOSED** (H10) | — |
| SECTION mixing FRAMEs + COMPONENT_SETs drops components | **CLOSED** (H11) | — |
| Doctor has no partial-pull check | **CLOSED** (H12) | — |
| `skip_pages` glob is caller-side; new separator patterns from designers (e.g. `---` rows) join the partial-pull set | low | Caller can extend its own glob without a figmaclaw release |
| `compute_page_hash` change is backwards-incompatible for pre-v9 hashes | none — intended | Schema bumps v7→v8 and v8→v9 force re-pull exactly once per file; page hashes converge on the new code immediately |

## Recommended follow-ups (for after PR 129 merges)

Most of the original follow-ups were rolled INTO this PR after the
user said "no deferring, all must be fixed now":

1. ~~Variant-content-change detection~~ — **closed in v9 (H8)**.
2. ~~`figmaclaw doctor` partial-pull check~~ — **closed (H12)**.
3. ~~Component frame-hash coverage~~ — **closed in v9 (H9)**.
4. ~~Mixed-SECTION component drop~~ — **closed (H11)**.
5. ~~Legacy synthetic file orphan cleanup~~ — **closed (H10)**.

What remains:

1. **smoke-api-ci failure** (separate from PR 129 — was failing before
   this PR; smoke job seems to need an API key in CI that isn't
   configured for PR runs). Worth investigating before merge.
2. **Per-variant content hash** (one level deeper than per-COMPONENT_SET).
   Currently `compute_frame_hash` on a COMPONENT_SET hashes its visible
   children's name+type, which catches variant rename/visibility but not
   variant *inner* layout changes. Surgical for variant-table
   re-enrichment. Estimated 2-3 hours but very low priority.

## Bug-hunt loop discoveries (post v8 hardening)

The user's "no deferring, all must be fixed now / continue fixing and
finding more in a loop" directive prompted a second pass that surfaced
five additional bugs beyond the original H1-H7 surface. These are
worth noting because their detection mode is generalisable:

| Bug | How it was caught |
|---|---|
| H8 (variant additions don't bump page_hash) | Reading `compute_page_hash` with edge-case eyes — "what does this not see?" The function only emitted one tuple per COMPONENT_SET, never descended. Tested by writing the test that would fail under the bug, then fixing. |
| H9 (compute_frame_hashes empty for components) | Reading `compute_frame_hashes` alongside `stale_frame_ids` — tracing what staleness detection would actually fire on for a component-only page. Answer: never. |
| H10 (legacy collision file as undetectable orphan) | Reading `prune_utils._NODE_SUFFIX_RE` and asking "what files does this regex actively reject?" The legacy synthetic filename, which has no digit-digit suffix, is rejected. Cross-checked against pull_logic's migration code which uses the same regex via `_node_suffix_from_relpath`. |
| H11 (mixed SECTION drops components) | Reading the existing test `test_section_with_both_frames_and_components_classifies_as_screen` and asking why this is the desired behavior. It isn't — it's data loss. The test was pinning a bug, not a rule. |
| H12 (doctor doesn't surface partial-pulls) | "What would have caught the original 215-page bug 6 months earlier?" Doctor would have, if it knew the shape. Adding the check is cheap. |

Detection lessons:
1. **Tests can pin bugs as "rules"** — read them sceptically. The
   single-line comment "Frames win: only the FRAME is rendered as a
   row, not the COMPONENT_SET" was load-bearing data loss.
2. **Regex allowlists silently exclude.** Any path-classifier regex
   that rejects valid input is a bug source. Walk every literal
   filename the codebase ever wrote and check membership.
3. **Two correct unit tests can sandwich a wrong integration.** H6
   was the canonical case; H10 was its echo at the path layer. If the
   test surface only checks "in-memory id is unique", the file path
   surface can still collide.
4. **"Rare in practice" is not the same as "won't happen".** H8 and
   H11 both involve user shapes designers actually adopt; the bug had
   probably been silently dropping data for years. The 215-page
   manifest backlog was the visible symptom.

## Honest limitations

* Unit tests don't replace a live CI run. The proof that the H2 fix
  actually heals linear-git's 215 partial-pull pages is the post-merge
  cron sequence draining the backlog, not the in-process tests. The
  unit tests prove "given this shape of page node, the new code
  produces a valid manifest entry"; CI proves "the new code, deployed,
  drains the backlog".
* The two GAP tests document real pre-existing partial-update bugs.
  Closing them is real work and was deferred from PR 129's scope.
  Filing as separate issues is the right move.
* Agent A did not touch the variables/MCP pipeline (B's lane). Any
  variables-side regression is B's surface, not pinned by agent A's
  tests except where overlap was natural (e.g. catalog idempotency
  under unavailable).

## Files touched (agent A's commits)

| Path | Change |
|---|---|
| `figmaclaw/figma_schema.py` | new constants `UNGROUPED_COMPONENTS_SECTION`, `UNGROUPED_COMPONENTS_NODE_ID` |
| `figmaclaw/figma_models.py` | top-level `is_component(child)` branch in `from_page_node`; page-scoped synthetic id; mixed-SECTION sibling synthetic emission (H11) |
| `figmaclaw/figma_hash.py` | `compute_page_hash` includes COMPONENT/COMPONENT_SET + variants; `compute_frame_hashes` covers component nodes (H8/H9) |
| `figmaclaw/figma_frontmatter.py` | `CURRENT_PULL_SCHEMA_VERSION: int = 9` + changelog |
| `figmaclaw/prune_utils.py` | exact-match allowlist for legacy collision file (H10) |
| `figmaclaw/pull_logic.py` | legacy → per-page synthetic migration on v8→v9 transition (H10) |
| `figmaclaw/commands/doctor.py` | partial-pull detection in manifest walk (H12) |
| `tests/test_unavailable_idempotency.py` | new — H1 regression suite |
| `tests/test_top_level_component_pages.py` | new — H2/H6 regression suite |
| `tests/test_pull_logic.py` | TESTED_UPGRADE_FROM_VERSIONS += {7, 8}; new v7→v8 and v8→v9 convergence tests |
| `tests/test_pr_129_adversarial.py` | 30+ adversarial tests covering H8-H11 |
| `tests/test_doctor.py` | partial-pull positive + negative tests |
| `docs/pr-129-investigation-agent-A.md` | working notebook (H1-H13 hypotheses, status, evidence) |
| `docs/pr-129-final-report-agent-A.md` | this file |
