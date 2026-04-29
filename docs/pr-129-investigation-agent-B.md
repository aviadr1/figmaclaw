# PR 129 investigation — agent B

> Coordination lane for the CI/idempotency/token-catalog consumer contract work.
> Agent A owns the partial-pull/component-page investigation in
> `docs/pr-129-investigation-agent-A.md`. This file tracks the separate
> hypothesis route so the two lanes complement each other.

## Workstream lane

Adversarial testing of PR 129 through the real `linear-git` consumer shape:

* legacy `ds_catalog.json` v1 data must migrate without losing seeded bridge
  entries or observed VariableID evidence;
* observation-only migrated catalogs must not be accepted as authoritative
  token definitions;
* `figmaclaw variables` must be idempotent when Figma variable definitions are
  unavailable in CI;
* reusable workflows must survive concurrent pushes from variables, census, and
  enrichment jobs;
* CI must have a way to fail loudly when a run requires fully articulated
  authoritative design-token definitions.

## Hypotheses, status, results

### B1 — legacy linear-git catalogs can migrate without data loss

* **Status.** Done.
* **Result.** `tests/test_linear_git_consumer_contract.py` pins migration from
  schema v1 to v2, preserving `SEEDED:*` entries and observed VariableID usage.
* **Commit.** `509bfae`.

### B2 — observation-only catalogs must not drive token suggestions

* **Status.** Done.
* **Result.** `suggest-tokens` refuses migrated observation-only data until the
  file has a current authoritative registry from `figmaclaw variables`.
* **Commit.** `509bfae`.

### B3 — unavailable variable definitions must not create nightly churn

* **Status.** Done.
* **Result.** Repeated unavailable verdicts no-op for the same file version, and
  timestamp-only nested `fetched_at` changes are ignored recursively.
* **Commits.** `f7cee67`, `d877bd2`.

### B4 — variables workflow must not lose work on concurrent pushes

* **Status.** Hardened after live CI conflict.
* **Result.** The first fix changed rejected-push recovery from `--ff-only` to a
  merge pull. A later `linear-git` run proved that is still insufficient for the
  generated `.figma-sync/ds_catalog.json`: the merge can conflict. The reusable
  variables workflow now treats rejected pushes as stale generated output,
  resets only the ephemeral CI checkout to the newest remote branch, reruns the
  deterministic variables refresh, and pushes the recomputed catalog.
* **Commits.** `f7cee67` plus the follow-up workflow replay fix.

### B5 — missing MCP credentials should not repeat failed fallback work per file

* **Status.** Done.
* **Result.** Auto mode caches the missing-MCP fallback verdict for the current
  command run. REST is still checked per file, but the impossible MCP fallback
  is attempted once, not once per tracked file.
* **Commit.** `556538a`.

### B6 — CI needs an explicit proof gate for authoritative token definitions

* **Status.** Implemented in this lane.
* **Result.** `figmaclaw variables --require-authoritative` and the matching
  reusable-workflow input fail if selected files only have `source=unavailable`
  markers or zero authoritative definitions. This keeps graceful fallback as
  the default while giving PR smoke runs a hard red signal when the secrets are
  not provisioned.
* **Evidence.** The existing `linear-git` PR smoke run failed in `source=mcp`
  because `FIGMA_MCP_TOKEN` is not configured. The new guard makes the same
  requirement expressible for normal variables workflow calls.

### B7 — live smoke tests must distinguish transient setup failure from invariant failure

* **Status.** Hardened after PR CI on `3bb07ff`.
* **Result.** Figma live smoke jobs exposed two external transient paths: REST
  page fetches can still return 429 after client retries, and MCP variable
  export can return the plugin-runtime read-only error for a single attempt.
  The MCP variables export now retries the read-only transient. Live API smoke
  assertions now skip only when page errors make the setup/verification
  inconclusive; completed live data still has strict assertions.
* **Commits.** pending follow-up.

## Current external blocker

The `linear-git` PR-wired branch currently has a schema-v2 catalog, but all
tracked libraries are marked `source=unavailable` because the repository secrets
do not include `FIGMA_VARIABLES_TOKEN` with `file_variables:read` scope or a
working `FIGMA_MCP_TOKEN`. PR 129 now preserves fallback data, avoids repeated
work, and can fail loudly when authoritative tokens are required; it cannot
materialize Figma variable names and values without one of those credentials.
