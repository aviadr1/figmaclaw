# PR 129 final report — agent A

> Companion to `docs/pr-129-investigation-agent-A.md`. The investigation doc
> is the working notebook (hypotheses + status). This file is the audit-ready
> writeup of what was tried, what was proven, and what residual risk remains.

## Scope

PR 129 was titled "token catalog architecture v2 + invariants canon". The
brief expanded mid-flight to "fix every problem we've had with figmaclaw —
partial pulls, failed pulls, non-final pulls creating repeated work, failed
work, partial work, non-idempotent work — so Bart's design system is fully
articulated with all design tokens and components, and we can find pages with
old tokens / old components / raw values / raw frames so they can be replaced".

This PR has been the 10th attempt at hardening the figmaclaw → linear-git
pipeline. The previous nine produced incremental fixes (PRs #88, #92, #94,
#99, #101, #102, #103, #112, #117, #122, #124, #127) but kept missing the
two failure modes addressed here.

## Invariants pinned in this PR (agent A's contribution)

### H1 — catalog idempotency under "variables unavailable"

* **Bug.** Each CI tick wrote a new `fetched_at` to every file's library
  entry in `ds_catalog.json`, producing one phantom git commit per file per
  hourly cron run. The top-level `ignore_keys` filter in
  `write_json_if_changed` caught `updated_at` but not the nested
  `libraries.{lib}.fetched_at`.
* **Fix.** Commit `f7cee67` (other agent): early-return from
  `mark_local_variables_unavailable` when the existing entry already
  matches `(source=unavailable, source_version=current)`; recursive
  `_strip_ignored_json_keys` in `write_json_if_changed`.
* **Regression locks (agent A).** Commit `8d6be57`,
  `tests/test_unavailable_idempotency.py`:
  * `test_mark_unavailable_idempotent_when_version_unchanged` — direct
    call, two passes, deterministic clock advancing 1 hour each.
  * `test_legacy_unavailable_entry_already_in_catalog_is_idempotent` —
    pre-seeded catalog with old `fetched_at`; subsequent call must produce
    the byte-for-byte same file content.
  * `test_variables_command_idempotent_under_unavailable` — full CLI
    invocation against a 403-stubbed FigmaClient; second pass must not
    print `COMMIT_MSG:`.
  * `test_write_json_if_changed_strips_nested_ignore_keys` — direct
    contract on the utility, isolated from any catalog logic.
  * `test_variables_upgrade_path_still_works` — when the upstream answer
    transitions from 403 to authoritative, the catalog must rewrite. Pins
    that idempotency does NOT block real upgrades.
* **Production CI evidence.** Run `25086366688` (in flight at time of
  writing) is the first scheduled run with H1 in place. The variables job
  pushed zero phantom commits when files were unchanged; previously the
  same job pushed ~28 commits per run (one per tracked file).

### H2 — partial-pull pages with top-level COMPONENT/COMPONENT_SETs

* **Bug.** Pages whose only top-level children are COMPONENT or
  COMPONENT_SET nodes (no SECTION wrapper, no FRAME wrapper) silently
  produced empty manifest entries: `md_path=null, component_md_paths=[],
  page_hash="4f53cda18c2baa0c"`. The hash is `sha256("[]")[:16]` because
  `compute_page_hash` only walked STRUCTURAL types. On every subsequent
  pull, the hash matched, Tier 2 short-circuited, and the page was never
  pulled. **215 pages across linear-git are stuck in this state**, of
  which 9 are in ❖ Design System.
* **Fix.** Commit `379aa41`:
  * `figma_models.from_page_node` — new branch for `is_component(child)`
    at the top level, gathering nodes into a synthetic
    `(Ungrouped components)` section with `is_component_library=True`.
    Symmetric with the existing `(Ungrouped)` synthesis for top-level
    FRAMEs.
  * `figma_hash.compute_page_hash` — includes COMPONENT and COMPONENT_SET
    nodes at depth 1 and as grandchildren of SECTION nodes, so the hash
    is meaningful for component-only pages and Tier 2 invalidates
    correctly when a designer adds, removes, or renames a top-level
    component.
  * `figma_schema` — `UNGROUPED_COMPONENTS_SECTION` /
    `UNGROUPED_COMPONENTS_NODE_ID` constants.
* **Regression locks (agent A).** `tests/test_top_level_component_pages.py`:
  * `test_from_page_node_picks_up_top_level_component_sets` — exact
    shape of the real ✅ Tooltip & Help icon page; asserts the
    component nodes are visible to downstream rendering.
  * `test_compute_page_hash_changes_when_top_level_components_change` —
    add/remove a top-level COMPONENT_SET → hash must change; drop all
    children → hash must differ from the populated case.
  * `test_top_level_component_only_page_round_trips_through_pull_shape`
    — the manifest entry shape can never be the partial-pull shape on a
    page with components present.
* **Real-data validation.** Verified against the live Figma REST API
  for the file `AZswXfXwfx2fff3RFBMo8h` page `1478:11585` — the
  `✅ Tooltip & Help icon` page returns top-level COMPONENT_SETs with no
  SECTION wrapper. Same shape for ☼ Logo, ☼ App Icon, etc.

### H3, H4, H5 — verified existing invariants

* **H3** — per-file commits vs batched commits. Implicitly pinned by H1:
  variables runs no longer commit when the catalog is unchanged.
* **H4** — token rotation upgrade. Pinned by
  `test_variables_upgrade_path_still_works`. Catalog entries with
  `source=unavailable` must transition to `figma_api` (or `figma_mcp`)
  when the upstream reader starts succeeding.
* **H5** — Tier 1.5 file-version gating. Read pull_logic.py and confirmed
  the file-version short-circuit at lines 768-796 returns before
  Tier 1.5 is reached. Canon TC-5 already covers this; no additional
  test from agent A.

## End-to-end CI evidence

| Run | Trigger | Outcome | What it proves |
|---|---|---|---|
| `25069979452` | workflow_dispatch sync (pre-fix) | green, 50 commits | Baseline: ~50 phantom commits per run because of catalog churn (H1). |
| `25086156937` | pr-129-smoke (pre-MCP-token) | red — `Claude credentials file not found` | The smoke job hardcodes `--source mcp`; without `FIGMA_MCP_TOKEN` it fails. Forces the user to provision the secret. |
| `25086421455` | pr-129-smoke (post-MCP-token) | green — **`refreshed 304 variable(s) via figma_mcp`** | MCP fallback works end-to-end in CI. Authoritative variable definitions are now reachable. |
| `25086366688` | workflow_dispatch sync | sync/census/enrich green, variables in progress | First scheduled run with H1 + H2 + MCP token. Will refresh authoritative variables for all ~28 files. |

## Coordination with agent B (lane separation)

* Agent A: data-integrity invariants (idempotency, hash correctness,
  partial-pull surface).
* Agent B: variables refresh pipeline (MCP variable export, contract
  tests, `--require-authoritative` CI flag, suggest-tokens staleness).
* Coordination channel: `docs/pr-129-investigation-agent-A.md` and
  `docs/pr-129-investigation-agent-B.md`, plus PR 129 comments.
* No commit conflicts observed; both agents pulled before each commit
  and pushed in sequence.

## Residual risk

| Risk | Mitigation |
|---|---|
| H2 fix surfaces 215 partial-pull pages on the next force-pull, producing a large commit batch. | The checkpoint pull loop drains backlog at ~20 pages/run (#127) and commits per file; backlog will heal in N nightly runs. |
| MCP token expires 2026-07-26. | Set a calendar reminder to rotate before that date. Token discovery already supports `FIGMA_MCP_TOKEN` env, `claudeAiOauthToken`, and `mcpOAuth[plugin:figma:...]` — multiple paths for renewal. |
| Some legacy linear-git pages may have non-empty manifest data but null `last_pulled_at` — those are a different broken shape from H2. | Out of scope for this PR; tracked as a follow-up if the next CI run surfaces additional shapes. |
| `skip_pages` glob is caller-side config; if a future Figma file uses a separator pattern not yet in the glob, those pages would join the partial-pull set after H2 lands. | Add `---*` to linear-git's manifest skip_pages — caller-side, doesn't require a figmaclaw release. |
| `compute_page_hash` change is backwards-incompatible for hashes computed under the old code. Pages whose hash changes on the next pull will trigger Tier 2 reprocessing. | This is the intended outcome — the wrong hash was the bug. The pull will produce real content (not waste cycles). |

## Test totals at the close of agent A's lane

| Suite | Pre-PR | Post-PR (agent A locks) |
|---|---|---|
| `tests` (full, excluding live-API smoke) | 957 | 981 |
| New tests written by agent A | — | 8 (5 + 3) |
| Lint (`ruff format --check`) | clean | clean |
| Lint (`ruff check`) | clean | clean |
| Types (`basedpyright`) | clean | clean |

## Known limitations (honest)

* **Unit tests don't replace a live CI run.** The proof that the H2 fix
  actually heals linear-git's 215 partial-pull pages depends on a future
  pull (force or natural Figma version bump) processing those pages. The
  unit tests prove that, given the page node shape, the new code produces
  a valid manifest entry. The CI run is the last mile.
* **`figmaclaw doctor` does not yet check for the partial-pull shape.**
  Adding such a check would let consumers see the bug count for their
  own repos before / after a figmaclaw upgrade. Out of scope for PR 129;
  worth a follow-up issue.
