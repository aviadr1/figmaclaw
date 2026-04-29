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

* **Hypothesis.** The Gigaverse design system file (`AZswXfXwfx2fff3RFBMo8h`) has 32 tracked pages in `manifest.files[k].pages`, but 14 of them have `md_path: null`. Of those:
  * 3 are correctly skipped by `skip_pages` glob (`---`).
  * 2 are component-only pages with populated `component_md_paths` (`✅ Avatar`, `✅ Avatar group`).
  * **9 are partial-pull artifacts**: `md_path: null` AND `component_md_paths: []` AND name does not match any `skip_pages` pattern. Examples: `☼ Logo`, `✅ Tooltip & Help icon`, `☼ App Icon`, `Textarea`, `Code`, `File organization`.
* **Why this matters for the user.** "✅ Tooltip & Help icon" publishes the `Tooltip` and `Help icon` component sets per `_census.md`. The page is real, has content, but no local mirror exists. Bart cannot work with the new design system "fully articulated" while these pages are missing.
* **Suspected root cause.** Either:
  1. an early sync run fetched these pages, ran into an error (timeout? screenshot? section), recorded the manifest entry, and never wrote the `.md` file; OR
  2. these pages had no "screen content" sections at the time of pull and `pull_logic._select_screen_section` returned None, leaving `md_path` null without writing component md.
* **Status.** Investigation in progress — see `manifest-page-classification` smoke harness below.
* **Plan.**
  1. Read `pull_logic` to find every code path that produces a `PageEntry` with `md_path=None`. Determine which paths also produce empty `component_md_paths`.
  2. Add a smoke test against the real linear-git manifest snapshot that classifies each tracked page and fails on any "partial" entry.
  3. Add a runtime invariant in `pull_logic` after page processing: a page entry must be one of (a) skipped-by-glob, (b) has md_path, (c) has component_md_paths. Anything else is a write of an inconsistent entry — heal-at-write per HE-1.
  4. Re-pull the affected pages on the linear-git test branch and verify the count of partial entries goes to zero.

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
| H1 | Idempotency under unavailable | done — regression locked |
| H2 | Partial-pull `md_path:null` pages | investigating |
| H3 | Per-file commits vs batched commits | pending CI run |
| H4 | Token rotation upgrade | pinned |
| H5 | Tier 1.5 file-version gating | pinned |

## Cross-lane TLDR (for agent B and the user)

* The catalog churn bug is fixed. Five new regression tests pin every dimension I could think of; the deterministic clock fixture means same-second timestamp collisions can't hide a regression.
* The actual-deployed CI path (`source=unavailable` for everything) is now stable — zero commits on no-op nightly runs.
* The next thing that will go wrong is the partial-pull pages. The user's stated goal — "Bart's new design system fully articulated, with all design tokens and components" — depends on the Tooltip / Help icon / Logo pages being present locally. They aren't, and the manifest evidence shows it's a real partial state, not an intentional skip.
* Variable definition NAMES will only populate when a `FIGMA_VARIABLES_TOKEN` (with Enterprise `file_variables:read`) is added to the linear-git repo secrets, OR a long-lived `FIGMA_MCP_TOKEN` is wired in. This is a deployment / secret-provisioning task, not a code task. The fall-back behavior is correctly graceful (seeded entries preserved, unavailable libraries marked, no churn) — this PR makes the fallback idempotent, but it does not invent variable names out of thin air.
