# PR 129 investigation — agent A

> **Coordination note.** Two Claude agents are working on PR 129 in parallel.
> This file is agent A's work route. The other agent's hypotheses live in a
> sibling doc (`pr-129-investigation-agent-B.md` if/when it is created) so we
> don't double-cover the same surface.
>
> Editing rule: agents add to their own file; cross-link via TLDR sections
> when a finding crosses into the other lane. The PR description and PR
> comments are the canonical "we both agree" channel.

## Workstream lane (agent A)

Adversarial regression testing of the **production CI deployment shape**:

* `FIGMA_API_KEY` lacks `file_variables:read` Enterprise scope → REST `/variables/local` returns 403.
* `FIGMA_MCP_TOKEN` is not set in linear-git → MCP fallback raises `FigmaMcpError` in CI.
* So the actually-deployed code path is `mark_local_variables_unavailable` for *every* tracked file, on every cron tick, indefinitely.

The other agent's lane (per recent commits): MCP variable export (chunking,
metadata guards, OAuth token discovery, contract tests against the real
linear-git fixture shape).

## Hypotheses, status, results

### H1 — `mark_local_variables_unavailable` is non-idempotent under "checked-and-still-403"

* **Hypothesis.** Repeated calls with the same `(file_key, file_version, source=unavailable)` rewrite the catalog because the per-library `fetched_at` is bumped on every call. `save_catalog` uses `write_json_if_changed` with top-level `ignore_keys`, but the nested `fetched_at` falls through.
* **Status.** Confirmed locally — running `figmaclaw variables --source rest --file-key AZswXfXwfx2fff3RFBMo8h` twice on the linear-git checkout produced an 8-line diff (only `updated_at` and the per-library `fetched_at`).
* **Result.** Fixed in commit `f7cee67` (other agent) by:
  1. early-returning from `mark_local_variables_unavailable` when the existing library entry is already `(source=unavailable, source_version=current)`;
  2. recursive `_strip_ignored_json_keys` in `figma_utils.write_json_if_changed`.
* **Regression lock.** `tests/test_unavailable_idempotency.py` (commit `8d6be57`, agent A): five tests covering the direct call, the CLI, the legacy-pre-existing-entry path, the nested ignore_keys mechanism, and the upgrade path (`unavailable` → `figma_api`).
* **Time-determinism note.** Tests use a `_SteppingClock` fixture that forces the patched `datetime.now` to advance by 1 hour per call so a same-second collision can't accidentally hide a regression.

### H2 — DS file has tracked pages with `md_path: null` AND no published components — "partial pull" artifact

**Status: ROOT CAUSE FOUND, FIXED, REGRESSION-LOCKED.**

* **Confirmed shape.** 9 of the 14 `md_path:null` DS pages share `page_hash: "4f53cda18c2baa0c"` and `frame_hashes_count: 0`. That hash is `sha256("[]")[:16]` — the canonical "empty list" digest.
* **Confirmed root cause.** Verified against the Figma REST API: ✅ Tooltip & Help icon (`1478:11585`) has top-level COMPONENT_SET children (`Tooltip` and presumably `Help icon`) directly under the CANVAS, with no SECTION wrapper. `from_page_node` only handled top-level SECTION and FRAME children. COMPONENT / COMPONENT_SET fell through the if/elif chain into the "skipped" comment branch → `sections=[]` → `screen_sections=[]` and `component_sections=[]` → manifest entry has `md_path=None, component_md_paths=[]`. `compute_page_hash` only walked STRUCTURAL types (FRAME/SECTION) → empty-list digest → Tier 2 short-circuits forever, page never re-pulled.
* **Fix.** Commit `379aa41`:
  * `figma_models.from_page_node`: synthesise an `(Ungrouped components)` section for top-level visible COMPONENT/COMPONENT_SET nodes, marked `is_component_library=True`. Symmetric with the existing `(Ungrouped)` synthesis for top-level FRAMEs.
  * `figma_hash.compute_page_hash`: include COMPONENT/COMPONENT_SET nodes at depth 1 and as grandchildren of SECTIONs.
  * `figma_schema`: new constants `UNGROUPED_COMPONENTS_SECTION` / `UNGROUPED_COMPONENTS_NODE_ID`.
* **Regression lock.** `tests/test_top_level_component_pages.py` — 3 tests with the real Tooltip-page shape, all failing before the commit and passing after. Full pytest run: 978 pass, 0 fail.
* **Expected impact in linear-git.** Once the fix lands and the next sync runs, the 9 partial-pull pages will produce a real component .md and a non-trivial page_hash (so Tier 2 will recognise content changes). Concretely the design system will gain local mirrors for `✅ Tooltip & Help icon`, `☼ Logo`, `☼ App Icon`, `☼ Date & Time Format`, `☼ Microcopy Guidelines`, `Textarea`, `Code`, `File organization`, `---------- IN PROGRESS` (this last one is intentionally a separator and should arguably be added to `skip_pages`; leaving as a follow-up).

### H2-followup — broaden `skip_pages` glob to catch hyphen-prefix separators

* **Hypothesis.** The skip_pages glob (`["old-*", "old *", "---"]`) misses
  hyphen-prefixed separator names like `---------- IN PROGRESS`. After
  H2's main fix lands, that page may produce a manifest entry; if it isn't
  meant to be tracked, broaden the glob to `["old-*", "old *", "---*"]`.
* **Status.** Open follow-up — caller-side configuration, not figmaclaw-side.

### H2-original-investigation-trail (kept for the audit log)

The original suspected causes were (1) early-failed pulls leaving null
md_path entries behind, or (2) pages with no screen content. Both turned
out to be wrong. The actual root cause was top-level COMPONENT/COMPONENT_SET
nodes falling through `from_page_node` and `compute_page_hash`. The audit
trail is preserved so the next contributor sees how the wrong hypothesis
was eliminated.

### H3 — `figmaclaw variables --auto-commit` produces N commits per run (one per tracked file) instead of one batched commit, even when nothing changed

* **Hypothesis.** Each iteration of the variables loop calls `git_commit` per file. On the test branch, runs that hit "unavailable" for ~50 files used to produce ~50 commits before the H1 fix. After H1, no commits should be produced when the catalog is unchanged. Verify the post-H1 deployment behavior is "zero commits when unchanged".
* **Status.** Pending verification on next test branch CI run.
* **Plan.** Trigger a workflow_dispatch on `test/figmaclaw-pr-129-ci`, observe variables job log: every file should print "variables unchanged" or "still unavailable", commit count should be 0, push step should be a no-op.

### H4 — Token-rotation upgrade is not blocked by the idempotency short-circuit

* **Hypothesis.** Once a real `FIGMA_VARIABLES_TOKEN` is provisioned (Enterprise scope), the very next variables run must transition every library entry from `source: "unavailable"` to `source: "figma_api"`, populate `name`, `values_by_mode`, modes, collections — and write the catalog.
* **Status.** Verified by the synthetic upgrade test `test_variables_upgrade_path_still_works`. The `_AuthClient` returns a real `LocalVariablesResponse` after a prior `_Unavailable403Client` run; the catalog rewrites and the library source becomes `figma_api`.
* **Result.** Pinned. The H1 short-circuit is keyed on `source == "unavailable" AND source_version == file_version`, so any change in either dimension forces re-fetch.

### H5 — Pull-time Tier 1.5 variables refresh is gated by file-version meta and only fires once per changed file

* **Hypothesis.** `pull_logic` enters `Tier 1.5` only for files whose `version` differs from the manifest. Re-running pull on an unchanged file skips Tier 1.5 entirely.
* **Status.** Read pull_logic.py:740-848 — confirmed the Tier 1 file-version short-circuit at line 768-796 returns before Tier 1.5 is reached.
* **Result.** Pinned by canon TC-5. No additional regression test from agent A — the canon test suite already covers it.

## Open agenda (agent A)

| ID | Title | State |
|---|---|---|
| H1 | Idempotency under unavailable | done — regression locked (5 tests, deterministic clock) |
| H2 | Partial-pull `md_path:null` pages | done — root cause identified (top-level COMPONENT_SETs), fixed in `379aa41`, regression locked (3 tests) |
| H3 | Per-file commits vs batched commits | pinned by H1 — no commits when nothing changed |
| H4 | Token rotation upgrade | pinned by `test_variables_upgrade_path_still_works` |
| H5 | Tier 1.5 file-version gating | pinned by canon TC-5; no additional test from agent A |
| H6 | MCP token availability in CI | resolved 2026-04-29 01:27 UTC by user; verified end-to-end in run `25086421455` (304 variables refreshed via figma_mcp) |
| H7 | Schema bump v7→v8 to force re-render of partial-pull pages | done in `a345741` — convergence test pinned for the v7→v8 transition |

## Quantitative evidence

### Before the H2 fix (latest `main` of linear-git, commit `598a26745`)
* 215 manifest entries with `(md_path=null, component_md_paths=[])` and `page_hash="4f53cda18c2baa0c"` across all tracked files.
* 9 of those are in `❖ Design System` (`AZswXfXwfx2fff3RFBMo8h`): ✅ Tooltip & Help icon, ☼ Logo, ☼ App Icon, ☼ Date & Time Format, ☼ Microcopy Guidelines, Textarea, Code, File organization, ---------- IN PROGRESS.
* `ds_catalog.json` schema_version=1, libraries={} (empty), 839 variables all with `name=null`.

### After the H1 fix (commit `f7cee67`, plus regression locks `8d6be57`)
* `mark_local_variables_unavailable` short-circuits when `(source=unavailable, source_version=current)`. No catalog write, no `fetched_at` bump.
* `write_json_if_changed` strips `ignore_keys` recursively, so nested `fetched_at` no longer triggers spurious writes.
* CI evidence: variables job log on run `25086366688` no longer carries 50 commit messages; net commit count for unchanged-state runs is 0.

### After the H2 fix (commit `379aa41`)
* `from_page_node` produces `(Ungrouped components)` synthetic section for top-level COMPONENT/COMPONENT_SETs.
* `compute_page_hash` includes COMPONENT/COMPONENT_SET nodes at depth 1 and grandchildren under SECTIONs.
* Once a future pull runs (either schema bump, `--force`, or natural Figma version change), the 215 partial-pull pages will produce real component .md files and non-trivial page hashes.

### After MCP token provisioning (2026-04-29 01:27 UTC)
* PR-129 smoke run `25086421455` succeeded: `TAP IN DESIGN SYSTEM: refreshed 304 variable(s) via figma_mcp`.
* Sync run `25086366688` started before the secret had fully propagated; first MCP call returned "credentials not found", subsequent calls were short-circuited by agent-B's per-run cache (`556538a`). Catalog churn was suppressed; no spurious commits.
* Sync run `25086592706` (started 01:38, after secret propagation): variables job refreshed authoritative names via MCP for the critical files:
  * **❖ Design System**: 345 variables, source=figma_mcp, source_version=2347065942124128936
  * **TAP IN DESIGN SYSTEM**: 304 variables (via the smoke job)
  * Branding: 162 variables
  * claude test: 327 variables
  * Archived Web: 6 variables
  * After ~5 minutes of MCP traffic, MCP started returning "credentials not found" again (likely Figma MCP rate-limit or session refresh issue) — agent-B's caching kicked in and the remaining files fell back to "kept seeded catalog fallback current" without churn.
* **Catalog state on `test/figmaclaw-pr-129-ci` after this run**: schema_version=2, **66 libraries**, **1511 variables**. Sample names from ❖ Design System: `color/neutral-light/0`, `color/surface/page` (light+dark modes), `color/border/default`, `color/bg/neutral/default`, `color/fg/neutral/strong`. Mode-aware values, proper hierarchical naming — Bart's actual published tokens.
* Sync run `25086906893` (in progress at time of writing): triggered after the schema bump landed in commit `a345741`. This is the first run where every file appears as `pull_schema_version<8` → schema-stale → forced re-render → top-level COMPONENT_SET pages get healed.

## Live CI evidence (in flight)

| Run | Trigger | Outcome | Notes |
|---|---|---|---|
| `25069979452` (2026-04-28) | workflow_dispatch | green | every variables file logged "definitions unavailable; kept seeded catalog fallback current" — ~50 phantom commits before the H1 fix |
| `25086156937` (2026-04-29 01:19) | pr-129-smoke-only | red | `MCP variables export failed — Claude credentials file not found ... Log in to Claude Code first or set FIGMA_MCP_TOKEN.` Smoke job is hardcoded to `--source mcp` and CI lacked the token. |
| `25086366688` (2026-04-29 01:27) | workflow_dispatch sync | running at time of writing | first run with the partial-pull fix (commit `379aa41`) — expect ~215 new component .md files for previously-empty pages across linear-git, of which ~9 are in ❖ Design System |
| (next) | workflow_dispatch sync | pending | will run after `FIGMA_MCP_TOKEN` is provisioned (just done by user, set 2026-04-29 01:27 UTC). Variables job should now produce authoritative library names via MCP. |

## Cross-lane TLDR (for agent B and the user)

* The catalog churn bug is fixed. Five new regression tests pin every dimension I could think of; the deterministic clock fixture means same-second timestamp collisions can't hide a regression.
* The actual-deployed CI path (`source=unavailable` for everything) is now stable — zero commits on no-op nightly runs.
* The next thing that will go wrong is the partial-pull pages. The user's stated goal — "Bart's new design system fully articulated, with all design tokens and components" — depends on the Tooltip / Help icon / Logo pages being present locally. They aren't, and the manifest evidence shows it's a real partial state, not an intentional skip.
* Variable definition NAMES will only populate when a `FIGMA_VARIABLES_TOKEN` (with Enterprise `file_variables:read`) is added to the linear-git repo secrets, OR a long-lived `FIGMA_MCP_TOKEN` is wired in. This is a deployment / secret-provisioning task, not a code task. The fall-back behavior is correctly graceful (seeded entries preserved, unavailable libraries marked, no churn) — this PR makes the fallback idempotent, but it does not invent variable names out of thin air.
