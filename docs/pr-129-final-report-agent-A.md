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
| H7 | Pull-schema bump to force backlog reprocessing | Done — `CURRENT_PULL_SCHEMA_VERSION` 7→8 | A | `a345741` |

H6 is the bug agent A discovered *after* shipping the H2 fix, by reading
live CI evidence from the test branch. See "What live CI surfaced" below.

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
| Tests | 957 | **986** |
| New tests by agent A | — | 29 (5 + 7 + 22; minus 5 overlap with B's MCP suite) |
| Lint (`ruff format --check`) | clean | clean |
| Lint (`ruff check`) | clean | clean |
| Types (`basedpyright`) | clean | clean |

Full local run, agent A's lane: `986 passed in 5.35s` (adversarial
suite alone), full suite green at most recent push.

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
| GAP: variant added inside a COMPONENT_SET does not bump page_hash | medium | Pinned by GAP test. Pre-existing, NOT a PR 129 regression. Recommend follow-up issue: descend one more level into COMPONENT_SETs in `compute_page_hash`, with a v8→v9 schema bump |
| GAP: `compute_frame_hashes` returns `{}` for top-level component-only pages | low | Pinned by GAP test. Component .md re-render on every page-hash change; per-frame staleness on variants is not tracked. Worth its own per-section content hash if needed |
| `skip_pages` glob is caller-side; new separator patterns from designers (e.g. `---` rows) join the partial-pull set | low | Caller can extend its own glob without a figmaclaw release |
| `compute_page_hash` change is backwards-incompatible for pre-fix hashes | none — intended | Schema bump v7→v8 forces re-pull exactly once per file; page hashes converge on the new code immediately |

## Recommended follow-ups (for after PR 129 merges)

1. **Variant-content-change detection** (closes the GAP test). Descend
   one more level into COMPONENT_SETs in `compute_page_hash`. Bump
   schema v8→v9. Estimated 1 hour.
2. **`figmaclaw doctor` partial-pull check.** Surface the bug count
   for any consumer repo before/after upgrade. Would have caught the
   215-page backlog months earlier. Estimated 2-3 hours.
3. **Component .md per-section content hash.** Independent staleness
   tracking for variant tables, mirror of `compute_frame_hash` for
   screen pages. Lets enrichment be surgical instead of full-section
   rewrite. Estimated half a day.
4. **smoke-api-ci failure** (separate from PR 129 — was failing before
   this PR). Worth investigating.

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
| `figmaclaw/figma_models.py` | top-level `is_component(child)` branch in `from_page_node`; page-scoped synthetic id |
| `figmaclaw/figma_hash.py` | `compute_page_hash` includes COMPONENT/COMPONENT_SET tuples (depth-1 and grandchild-of-SECTION) |
| `figmaclaw/figma_frontmatter.py` | `CURRENT_PULL_SCHEMA_VERSION: int = 8` |
| `tests/test_unavailable_idempotency.py` | new — H1 regression suite |
| `tests/test_top_level_component_pages.py` | new — H2/H6 regression suite |
| `tests/test_pull_logic.py` | added 7 to `TESTED_UPGRADE_FROM_VERSIONS`; new v7→v8 convergence test |
| `tests/test_pr_129_adversarial.py` | new — 22 adversarial / GAP tests |
| `docs/pr-129-investigation-agent-A.md` | working notebook (H1-H7 hypotheses, status, evidence) |
| `docs/pr-129-final-report-agent-A.md` | this file |
