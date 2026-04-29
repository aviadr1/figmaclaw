# PR 129 final report — agent B

> Companion to `docs/pr-129-investigation-agent-B.md` and Agent A's
> `docs/pr-129-final-report-agent-A.md`. This report covers the consumer
> contract, token-catalog, CI idempotency, and workflow finality lane.

## Scope

This lane tested PR 129 against the real `linear-git` consumer shape rather
than only against isolated figmaclaw fixtures. The goal was to prove that
figmaclaw can repeatedly pull, refresh variables, and feed token suggestions
without partial state, repeated work, stale observation-only data, or lost CI
work.

The critical consumer requirement is that Bart's current design system tokens
are represented by authoritative catalog entries, and that old tokens, old
components, raw values, and raw frames can be found from generated frontmatter
and sidecars without parsing markdown prose.

## Invariants pinned in this lane

### B1 — legacy linear-git catalogs migrate without data loss

* **Bug class.** The existing `linear-git` catalog had schema-v1 seeded bridge
  entries and observed VariableID evidence. A migration that treats that data as
  authoritative would either lose the bridge data or feed untrusted suggestions.
* **Fix / lock.** `tests/test_linear_git_consumer_contract.py` now migrates a
  realistic legacy catalog and asserts that `SEEDED:*` bridge variables and
  observed usage evidence survive the schema-v2 upgrade.
* **Commit.** `509bfae`.

### B2 — observation-only catalogs cannot drive token suggestions

* **Bug class.** A migrated catalog with observed VariableID evidence but no
  current variable definitions can look non-empty and accidentally drive
  `suggest-tokens`.
* **Fix / lock.** `suggest-tokens` refuses observation-only migrated catalogs
  until `figmaclaw variables` refreshes authoritative definitions for the file.
* **Commit.** `509bfae`.

### B3 — unavailable variable definitions are idempotent

* **Bug class.** When REST `/variables/local` is unavailable and MCP is missing,
  every run used to rewrite nested `fetched_at` timestamps and create repeated
  catalog churn.
* **Fix / lock.** Same file/version/source unavailable verdicts are no-ops, and
  ignored JSON keys are stripped recursively before write comparison.
* **Commits.** `f7cee67`, `d877bd2`.

### B4 — missing MCP credentials are not retried for every file

* **Bug class.** With no MCP credential in CI, the variables job repeated the
  same impossible fallback once per tracked file.
* **Fix / lock.** Auto mode caches only persistent MCP credential/configuration
  failures for the current command invocation. REST still runs per file.
* **Commit.** `556538a`.

### B5 — transient MCP failures do not poison later files

* **Bug class found by live CI.** A full `linear-git` run refreshed several
  files through MCP, then one file failed with a plugin-runtime read-only error.
  The previous cache treated that transient per-file error like missing
  credentials and skipped MCP for all later files.
* **Fix / lock.** Only persistent missing-token / missing-credential errors are
  cached across files. Transient MCP export errors mark that one file
  unavailable and the next file retries MCP.
* **Commit.** `19cd1c0`.

### B6 — CI can require authoritative design-token definitions

* **Bug class.** Graceful fallback is correct for normal consumers, but this PR's
  proof target needs a hard red signal if the catalog is only
  `source=unavailable`.
* **Fix / lock.** `figmaclaw variables --require-authoritative` and the reusable
  workflow input fail unless the selected files contain authoritative
  `figma_api` or `figma_mcp` definitions.
* **Commit.** `a1151b9`.

### B7 — rejected variables pushes replay generated output instead of merging it

* **Bug class found by live CI.** The first rejected-push fix used a merge pull.
  A later `linear-git` run proved generated `.figma-sync/ds_catalog.json` can
  still conflict under concurrent variables/census/enrichment pushes.
* **Fix / lock.** On rejected push, the reusable variables workflow resets only
  the stale ephemeral CI checkout to the newest remote branch, reruns the same
  deterministic variables command with the same inputs/secrets, then pushes the
  recomputed catalog.
* **Commit.** `60bfbb4`.

### B8 — live Figma API smoke does not report false schema regressions on 429

* **Bug class found by PR CI.** A live API 429 during schema-upgrade setup made
  the no-body-rewrite assertion inconclusive but failed as if the invariant had
  regressed.
* **Fix / lock.** The smoke skips that specific inconclusive case when page
  errors occur, while still asserting body preservation on successful live data.
* **Commit.** `60bfbb4`.

## Live CI evidence

| Run | Repo | Outcome | Evidence |
|---|---|---|---|
| `25086156937` | `linear-git` | red | Proved CI lacked `FIGMA_MCP_TOKEN`; MCP smoke failed before any token catalog proof could be trusted. |
| `25086421455` | `linear-git` | green | After secret provisioning, smoke refreshed Tap In DS with `304 variable(s) via figma_mcp`; `suggest-tokens` saw `1109 variables` and `no_match: 0` for the tested sidecar. |
| `25086592706` | `linear-git` | green with finding | Revealed transient MCP file errors were incorrectly cached across later files; fixed in `19cd1c0`. |
| `25093203810` | `linear-git` | red | Revealed merge-pull recovery can conflict on generated `ds_catalog.json`; fixed in `60bfbb4`. |
| `25093287379` | `linear-git` | green | Full workflow succeeded after transient-MCP fix; catalog reached schema v2, 66 libraries, 1807 variables, 52 MCP-authoritative libraries, 14 unavailable libraries, Tap In DS 304 variables. |
| `25097977016` | `figmaclaw` | green | PR CI on head `60bfbb4`: ruff, typecheck, tests, CodeQL, smoke-webhook, smoke-mcp, and smoke-api all passed. |
| `25098005988` | `linear-git` | in progress at report time | Full consumer proof run dispatched after `60bfbb4`; sync was still running while this report was written. |

## Local proof

* `uv run pytest tests/test_workflow_template_invariants.py tests/smoke/test_figma_api_smoke.py::test_schema_upgrade_backfills_instance_component_ids_without_body_rewrite -q` → 8 passed.
* `uv run ruff check .` → passed.
* `uv run basedpyright` → 0 errors.
* `uv run pytest -q` → 1007 passed, 1 skipped.
* MCP smoke with local `.env` loaded → 3 passed.

## Current catalog facts

The latest completed `linear-git` full run after MCP provisioning produced:

* schema version: 2
* libraries: 66
* variables: 1807
* authoritative MCP libraries: 52
* unavailable libraries: 14
* Tap In DS variables: 304
* sources present: `figma_mcp`, `unavailable`

This proves the main Tap In / Bart design-token requirement is now backed by
authoritative MCP definitions. It does not prove every tracked Figma file is
fully authoritative: 14 libraries still fall back to unavailable because Figma
MCP returned file-level errors or access limitations for those files.

## What this lane specifically proved

* Legacy catalog migration preserves old bridge data and does not promote it to
  authoritative data.
* Token suggestions require real refreshed variables, not observation-only
  leftovers.
* Repeated unavailable runs no-op.
* Missing MCP credentials no longer cause per-file repeated work.
* Transient MCP errors no longer stop later files from being attempted.
* CI has an explicit strict mode for authoritative-token proof.
* Variables workflow recovery is final under concurrent pushes because it
  recomputes generated state from the newest remote branch.
* The figmaclaw PR itself is green after the last live-smoke hardening.

## Residual risks and direct mitigations

| Risk | Mitigation |
|---|---|
| 14 tracked libraries still have `source=unavailable`. | Treat the new design-system / Tap In proof as complete, but audit the 14 files separately for access restrictions, file type limitations, or MCP plugin read-only errors. Use `--require-authoritative --file-key ...` for files that must be hard-gated. |
| Generated catalog conflicts can happen in other reusable workflows. | Apply the same replay-generated-output principle to any workflow that writes deterministic generated JSON under concurrent CI. Do not text-merge generated artifacts when a replay is available. |
| Live API smoke can still be affected by rate limits. | Keep smoke assertions strict when live data is complete, but mark rate-limited setup as inconclusive and add deterministic unit coverage for the invariant. |
| MCP token expires on 2026-07-26. | Rotate before expiry and keep `FIGMA_MCP_TOKEN` in both `linear-git` and `figmaclaw` repo secrets. |
| Authoritative coverage could regress silently if secrets are removed. | Enable `require_authoritative: true` on dedicated proof workflows, especially for Tap In DS and any design-system file used by token replacement automation. |

## Suggestions for Agent A / next adversarial scenarios

* Add a `figmaclaw doctor` or manifest audit check that reports pages with
  `md_path=null`, `component_md_paths=[]`, and the empty-list hash. That would
  have pre-found the partial-pull class before a user noticed missing pages.
* Add a catalog audit command that prints authoritative/unavailable counts by
  file key and fails when named critical files are not authoritative.
* Add a CI "double run" proof: run variables twice on the same checked-out
  `linear-git` fixture and assert the second run has no diff and no commit.
* Add a synthetic concurrent-push integration test around reusable workflow
  scripts, using a temporary bare repository, to prove replay recovery works
  without waiting for GitHub Actions to race naturally.
* Add a focused post-sync audit for `raw_frames`, `component_set_keys`, and
  legacy token sidecars: every changed Figma page should be classifiable as
  componentized, raw-frame-containing, old-token-containing, or clean.
* Add a PR checklist item that every graceful fallback also has a strict proof
  mode. The absence of that distinction is what let "green but unavailable"
  runs look acceptable earlier.

