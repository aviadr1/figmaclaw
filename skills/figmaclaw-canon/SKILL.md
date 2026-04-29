---
name: figmaclaw canon
description: Use when working with figmaclaw-generated data (figma/*.md pages, _census.md, ds_catalog.json, token sidecars) or modifying figmaclaw itself. Covers the four-layer data contract (frontmatter / body / manifest / file-scope registries), invariant classes (BP/SC/FM/CL/W/CR/KS/TS/CW/LW/HE/TC/TS-S/REG/PP/NC/HSH/SI/MIG/AUTH/ERR/WF), design decisions D1-D14, refresh-trigger ladder, and the failure-mode catalog F1-F20. Authoritative for "is this change safe?" questions.
---

# figmaclaw canon — invariants and design decisions

> **Status:** authoritative. This skill is the single source of truth for figmaclaw's data contract, invariants, and design decisions. Every other doc in figmaclaw's `docs/` directory either feeds into this one (historical context, deeper rationale) or is superseded by it. Module docstrings, commit-message conventions, `CLAUDE.md` (figmaclaw), and consumer-repo agent fragments cross-link here for the actual rule.

> When you change figmaclaw's behavior, the order of operations is: (1) update this skill, (2) update tests, (3) write code. If the canon doesn't authorize the behavior, don't ship it.

> **Consumers:** this skill is bundled with the figmaclaw plugin. Invoke it as `figmaclaw:figmaclaw canon` from any consumer repo — you do not need to read figmaclaw's `CLAUDE.md` separately.

## Table of contents

- [Philosophy](#philosophy)
1. [Four-layer data contract](#1-four-layer-data-contract)
2. [Storage-tier table](#2-storage-tier-table)
3. [Refresh-trigger ladder](#3-refresh-trigger-ladder)
4. [Invariant classes](#4-invariant-classes)
   - [BP — Body preservation](#bp--body-preservation)
   - [SC — Scaffold](#sc--scaffold)
   - [FM — Frontmatter correctness](#fm--frontmatter-correctness)
   - [CL — CLI flag innocence](#cl--cli-flag-innocence)
   - [W — Write idempotency](#w--write-idempotency)
   - [CR — Cross-run discipline](#cr--cross-run-discipline)
   - [KS — Frame-keyed key-set](#ks--frame-keyed-key-set)
   - [TS — Terminal-state for LLM-dispatched work](#ts--terminal-state-for-llm-dispatched-work)
   - [CW — Canonical walker reuse](#cw--canonical-walker-reuse)
   - [LW — Log-writer auto-heal](#lw--log-writer-auto-heal)
   - [HE — Heal-at-entry](#he--heal-at-entry)
   - [TC — Token catalog](#tc--token-catalog)
   - [TS-S — Token sidecar](#ts-s--token-sidecar)
   - [REG — File-registry state](#reg--file-registry-state)
   - [PP — Pull terminal state](#pp--pull-terminal-state)
   - [NC — Node coverage parity](#nc--node-coverage-parity)
   - [HSH — Hash coverage](#hsh--hash-coverage)
   - [SI — Synthetic identity](#si--synthetic-identity)
   - [MIG — Generated-artifact migration](#mig--generated-artifact-migration)
   - [AUTH — Authority claims](#auth--authority-claims)
   - [ERR — Failure scoping](#err--failure-scoping)
   - [WF — Workflow recovery](#wf--workflow-recovery)
5. [Design decisions D1..D14](#5-design-decisions)
6. [Failure-mode catalog](#6-failure-mode-catalog)
7. [Document index](#7-document-index)
8. [Anti-pattern checklist for PR review](#8-anti-pattern-checklist-for-pr-review)

---

## Philosophy

figmaclaw invariants exist to protect expensive, hard-won knowledge while still allowing cheap, incremental refresh. A correct change preserves human/LLM-authored data, never silently discards generated evidence that consumers rely on, knows the cost tier of every action, and can answer what is stale, why it is stale, and what minimum refresh will make it current.

Use this philosophy to decide whether a proposed rule belongs in canon:

- **Protect hard-won data.** Bodies, enrichment state, human choices, generated registries used by consumers, and migration evidence must not be lost silently. If data is costly to recreate, preserve it; if it is recomputable, make the recomputation path explicit.
- **Work incrementally.** Prefer the cheapest sufficient refresh tier. Do not make "get one current answer" require a full pull, screenshots, LLM enrichment, or repo-wide churn when a file-scope or page-scope refresh is enough.
- **Know freshness.** Every cache consumer must be able to answer whether its input is current. If it is stale, it should name what is stale, why it is stale, and the smallest action that will make it current.
- **Keep authority clear.** Separate source truth, generated cache, observed usage, advisory suggestions, and prose. Do not let observation, fallback state, or seeded bridge data masquerade as authoritative Figma truth.
- **Recover by preserving provenance.** When workflows race, APIs fail, or schemas change, recovery should restore a state that could have been produced from source data. Do not text-merge generated cache snapshots or leave legacy generated artifacts orphaned.

Each invariant below should serve one of these goals. If a proposed invariant only records a temporary implementation preference, put it in tests, docs, or the review checklist instead of canon.

## 1. Four-layer data contract

figmaclaw's data is organized in four layers. Every artifact figmaclaw writes belongs to exactly one of them, and each layer has a different authority and a different writer.

| Layer | Authority | Recomputable? | Who writes it | Examples |
|---|---|---|---|---|
| **Frontmatter** (in page `.md` files) | Source of truth for page identity, structure, and enrichment state. | No — losing it loses the page's identity in git. | figmaclaw CLI only. | `frames`, `flows`, `enriched_hash`, `enriched_frame_hashes`, `enriched_at`. |
| **Body** (in page `.md` files) | LLM/human-authored prose. | No — costs Figma screenshots + LLM inference + human review. | LLMs/humans only, via `write-body`. **Code never writes prose.** | Page summary, section intros, frame description tables, Mermaid charts. |
| **Manifest** (`.figma-sync/manifest.json`) | Sync-engine cache. | Yes — if deleted, sync re-fetches everything. Zero data loss. | figmaclaw CLI. | Per-file `version`, `last_modified`, `pull_schema_version`; per-page `page_hash`, `frame_hashes`. |
| **File-scope registries** (`.figma-sync/ds_catalog.json`, `figma/{slug}/_census.md`) | Authoritative cache of file-level Figma data. Recomputable from REST. | Yes — `figmaclaw variables` and `figmaclaw census` reproduce them in seconds. | figmaclaw CLI (dedicated subcommands). | Variable catalog, published component-set census. |

**The law:**

- Frontmatter is the index of what exists on a page. **Use it to make enrichment decisions cheaply (no API calls).**
- Body is prose. **No Python code, no CLI command, no agent tool may parse prose or use prose as source of truth.** Code may inspect canonical generated headings/tables only through the canonical walkers named in CW-1. No `parse_page_summary()`. No `parse_section_intros()`. No ad hoc regex over body tables.
- Manifest is engineering cache. Treat it as recomputable; never store load-bearing information in it that isn't reproducible from the API.
- File-scope registries are file-scope answers cached in committed files for cross-tool consumption (suggest-tokens, agent skills, CI). They are recomputable from REST and must remain so.

The full body-preservation argument and the full manifest-vs-frontmatter argument are derived from this contract. See [§4 BP, FM](#bp--body-preservation), [§4 W](#w--write-idempotency), [§5 D3, D4, D11](#5-design-decisions).

## 2. Storage-tier table

Every artifact figmaclaw produces, what it caches, when it refreshes, who writes it, and whether losing it is recoverable.

| Tier | Storage | What it caches | Refresh trigger | Writer | Recoverable from REST? |
|---|---|---|---|---|---|
| File meta | `manifest.files[k].version`, `last_modified`, `last_checked_at` | "Has the file changed?" | every `pull` (cheap meta call) | `pull_logic.py` | yes |
| File registry: components | `figma/{slug}/_census.md` | Published/importable component sets (name, key, page, updated). Stable content hash over `(name, key)` pairs. Local unpublished component definitions are page structure and render to `components/*.md`; they are not census entries. | `figmaclaw census` standalone. `pull` fetches component sets for `component_set_keys`, but does not write `_census.md`. | `commands/census.py` | yes |
| **File registry: variables** | `.figma-sync/ds_catalog.json` (schema v2) | Variable definitions per library: name, collection, resolved_type, values_by_mode, scopes, code_syntax, alias_of. | `figmaclaw variables` standalone, **and** opportunistic during `pull` when file version changed | `commands/variables.py`, `token_catalog.py` | yes (REST `/variables/local` when `file_variables:read` is available, or Figma MCP plugin-runtime export; `seeded:*` entries fill the gap when no authoritative reader is available) |
| Page structure | `figma/{slug}/pages/*.md` frontmatter (`frames`, `flows`); `manifest.files[k].pages[p].page_hash` / `frame_hashes` | Page node tree, prototype edges, hashes. | `pull` when file version changed AND page hash changed | `pull_logic.py`, `figma_render.py` | yes |
| Page tokens (raw/stale usage) | `figma/{slug}/pages/*.tokens.json` | Per-frame `(property, classification, value)` aggregates with `count`. Schema v2. | `pull` when page hash changed | `pull_logic.py`, `token_scan.py` | yes (re-walk page) |
| Page body | `figma/{slug}/pages/*.md` body | LLM-authored prose. | `claude-run` (LLM enrichment, downstream of pull) | LLM via `write-body`; figmaclaw never overwrites | **no — protected by BP-1..6** |
| Component section body | `figma/{slug}/components/*.md` body | LLM-authored prose for DS sections. | `claude-run` | Same as above. | **no — same** |

`page_hash` is **not** stored in `.md` frontmatter; only `enriched_hash` and `enriched_frame_hashes` are. Current hashes live in the manifest (see [D9](#5-design-decisions)).

## 3. Refresh-trigger ladder

When a sync runs, figmaclaw cascades through these checks. Higher tiers gate lower tiers — i.e. if a higher tier shows "no change," the work below is skipped.

```
┌──────────────────────────────────────────────────────────────────┐
│ Tier 1   File-version meta (`?depth=1`)                          │
│          stored.version == api_version  →  skip whole file       │
│          else:                                                    │
└──────────────────────┬───────────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────────┐
│ Tier 1.5 File-scope registry refresh                             │
│          `get_local_variables(file_key)` / MCP export → catalog   │
│          (TC-1, TC-5)                                             │
│          `get_component_sets(file_key)`  → component_set_keys    │
│          for component markdown. `pull` does not write census.   │
└──────────────────────┬───────────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────────┐
│ Tier 2   Per-page hash (computed from node tree)                 │
│          stored.page_hash == computed  →  skip page              │
│          else: re-render frontmatter + token sidecar             │
└──────────────────────┬───────────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────────┐
│ Tier 3   Per-frame hash (depth-1 children)                       │
│          frontmatter.enriched_frame_hashes[f] == computed        │
│          → that frame's body row is already current              │
│          Drives surgical re-enrichment; see D2, D7, D10.         │
└──────────────────────┬───────────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────────┐
│ Content hash (registry-internal, e.g. census)                    │
│          stable hash over (name, key) pairs                      │
│          rewrite the artifact only when hash changes             │
└──────────────────────────────────────────────────────────────────┘

╭──────────────────────────────────────────────────────────────────╮
│ Schema-version forced refresh                                    │
│ When CURRENT_PULL_SCHEMA_VERSION bumps, files at older versions  │
│ get re-rendered even if Figma is unchanged. Bypass-flag budget   │
│ rule applies (W-3 / anti-loop dim 1 corollary).                  │
╰──────────────────────────────────────────────────────────────────╯

╭──────────────────────────────────────────────────────────────────╮
│ Webhook + cron fallbacks                                         │
│ Webhook → incremental, file-scoped, fires on Figma save.         │
│ Cron / `--force` → catch-all for missed webhooks.                │
╰──────────────────────────────────────────────────────────────────╯
```

The Tier 1.5 slot is the architectural fix for failure-mode F1: file-scope content (variables, components, styles) refreshes once per changed file, decoupled from per-page hashing. See [D11](#5-design-decisions) and the audit in issue #128.

## 4. Invariant classes

Each class names a category of always-true property. The IDs are stable; tests and PR review reference them by ID. New invariants get appended; nothing is renumbered.

### BP — Body preservation

The `.md` body is LLM-authored prose. Producing it costs Figma screenshots + LLM inference + human review. Losing it silently is unacceptable.

| ID | Invariant | Enforced by |
|---|---|---|
| **BP-1** | `sync` on an existing file preserves the body byte-for-byte. | `tests/test_body_preservation.py::test_bp1_*` |
| **BP-2** | `pull_file` on an existing file preserves the body byte-for-byte. | `test_bp2_*` |
| **BP-3** | `set-flows` on an existing file preserves the body byte-for-byte. | `test_bp3_*` |
| **BP-4** | `update_page_frontmatter()` preserves the body byte-for-byte. | `test_bp4_*` |
| **BP-5** | `scaffold_page()` is never called on existing files by `sync` or `pull`. | `test_bp5_*` |
| **BP-6** | `write-body` preserves frontmatter byte-for-byte. | `test_bp6_*` |

Bonus stress tests pin "body survives 5 consecutive `sync` operations" and "body survives interleaved `sync` + `set-flows` cycles."

### SC — Scaffold

New files get proper LLM placeholders.

| ID | Invariant | Enforced by |
|---|---|---|
| **SC-1** | `sync` on a non-existent file writes a scaffold with LLM placeholders. | `test_sc1_*` |
| **SC-2** | `pull_file` on a non-existent file writes a scaffold with LLM placeholders. | `test_sc2_*` |
| **SC-3** | Scaffold contains `<!-- LLM: ... -->` placeholders for page summary, section intros, and Mermaid. | `test_sc3_*` |

### FM — Frontmatter correctness

| ID | Invariant | Enforced by |
|---|---|---|
| **FM-1** | Existing frame descriptions survive `sync`. | `test_fm1_*` |
| **FM-2** | Existing flows survive `sync`. | `test_fm2_*` |
| **FM-3** | New frames from Figma appear in frontmatter after `sync`. | `test_fm3_*` |
| **FM-4** | Frontmatter is valid `FigmaPageFrontmatter` after `sync`. | `test_fm4_*` |

### CL — CLI flag innocence

Informational flags never modify the file.

| ID | Invariant | Enforced by |
|---|---|---|
| **CL-1** | `--scaffold` prints to stdout without modifying the file. | `test_cl1_*` |
| **CL-2** | `--show-body` prints to stdout without modifying the file. | `test_cl2_*` |

### W — Write idempotency

Every function that writes a file must be idempotent: skip the write if only a timestamp (`generated_at`, `updated_at`, etc.) would change.

| ID | Invariant | Enforced by |
|---|---|---|
| **W-1** | All writers compare load-bearing content (timestamps stripped) before writing; no-op when unchanged. Reference implementations: `_write_token_sidecar`, `save_catalog`, `census._render`. | source-scan meta-test (commits `488788c`, `496cb43`); `tests/test_*idempotency*` |
| **W-2** | When a writer DOES write, the resulting file must round-trip through its own reader (e.g. census's `_existing_hash(out_path) == content_hash`, commit `f56eb17`). | runtime assertion in writer + golden test |
| **W-3** | **Bypass-flag budget rule.** Any flag that bypasses the page-hash check (`--force`, `schema_stale`) must NOT consume the `max_pages` budget for pages it processes. Otherwise the `while pull` loop never terminates. Schema-only upgrades are the canonical example. | `tests/test_*budget*`, commit `5612e2b` |

**Rationale.** figmaclaw runs in a CI loop. Any unconditional write — even a timestamp — lands in a git commit, triggers Claude enrichment, and wastes budget. Idempotency is the foundation of every other invariant in this file.

### CR — Cross-run discipline

Anti-loop dim 1.

| ID | Invariant | Enforced by |
|---|---|---|
| **CR-1** | Any guard that prevents retries within a run must be paired with an invariant that prevents retries across runs. Test shape: `state_0 := fixture; run code → state_1; run code → state_2`; assert `state_2` does not re-select work `state_1` already addressed. | `tests/test_*cross_run*` |
| **CR-2** | A consumer of a recomputable cache must detect staleness explicitly. If the cache's `source_version` is older than the upstream's current version (or absent), the consumer exits non-zero with an actionable message; it does NOT produce results from a stale cache. Applies to `suggest-tokens` reading `ds_catalog.json` and any analogous reader. | `tests/test_*staleness*` |

### KS — Frame-keyed key-set

Anti-loop dim 2.

| ID | Invariant | Enforced by |
|---|---|---|
| **KS-1** | For every frame-keyed dict in frontmatter (`enriched_frame_hashes`, `raw_frames`, `raw_tokens`, `frame_sections`, `unresolvable_frames`), `keys(d) ⊆ frames` must hold after every write. Enforced centrally in `figma_render._build_frontmatter` (single chokepoint) and defensively on parse by `FigmaPageFrontmatter._cap_unresolvable_frames_to_frames`. | `tests/test_frontmatter_key_set_invariant.py` |

**If you add a new frame-keyed field**: extend `_build_frontmatter` to prune it; add a test asserting orphan keys are dropped on write. Do not rely on callers to pre-prune.

### TS — Terminal-state for LLM-dispatched work

Anti-loop dim 3.

| ID | Invariant | Enforced by |
|---|---|---|
| **TS-1** | Every "pending" state has a terminal counterpart. If the LLM can produce an output that says "I cannot resolve this right now" (e.g. `(no screenshot available)`), that output must be recordable as a tombstone so we don't re-dispatch the same question on the next run. Tombstones auto-invalidate when the underlying content hash changes. | tombstone-protocol tests |

If you introduce a new "soft-done" row marker, design the tombstone protocol in the same PR.

### CW — Canonical walker reuse

Anti-loop dim 4.

| ID | Invariant | Enforced by |
|---|---|---|
| **CW-1** | Body frame-row iteration has exactly one canonical implementation: `body_validation.iter_body_frame_rows`. Fence-aware, exact rendered-header matching, yields `BodyFrameRow` pydantic models with `line_index` and `node_id`. Use it for any code that inspects or mutates canonical body frame tables. | `tests/test_body_validation.py` |

Don't re-implement `is_table_separator` / `parse_frame_row` / fence tracking in new code. Don't copy loops from existing walkers. Don't import `re` in pull / claude-run / write-body code to match row shapes.

Use `iter_body_frame_rows` for row-by-row work; `body_frame_node_ids` (a thin projection) when you only need the node-id list; `section_line_ranges` / `parse_sections` for section-level work.

### LW — Log-writer auto-heal

Anti-loop dim 5.

| ID | Invariant | Enforced by |
|---|---|---|
| **LW-1** | A log writer that emits "WARN … skipped until file is fixed" but never fixes the file silently loses data every run forever. Acceptable resolutions, in order of preference: (a) **Migrate** in place on the first mismatch; (b) **Auto-archive and reset** — rename the prior file to `<name>.bak.<UTC-timestamp><ext>` and start a fresh schema-v1 log, emit a human-readable error line; (c) **Hard-fail** for critical writers where silently resetting would corrupt load-bearing state. | `tests/test_*log_writer*` |
| **LW-2** | Schema migrations for sidecars and catalogs follow the same rule. v1 → v2 either migrates content forward (preserving any human-set fields like `fix_variable_id`) or auto-archives. Never silently overwrite. | `tests/test_*sidecar_migration*`, `tests/test_*catalog_migration*` |

The test shape that pins this: run 1 with bad input triggers the heal; run 2 with no new bad input does NOT re-emit the error.

### HE — Heal-at-entry

Anti-loop dim 6.

| ID | Invariant | Enforced by |
|---|---|---|
| **HE-1** | Every selection / entry boundary that reads a page `.md` calls `normalize_page_file` (or goes through something that does, e.g. `claude_run.enrichment_info`) as its first step. New entry points register in `_HEALING_ENTRY_POINTS` in the parametric test. | `tests/test_entry_point_heals.py` |

A structural invariant that fires only when WE write a file leaves files written by older figmaclaw, hand-edits, or merge-conflict resolutions in violating state. Heal on encounter is the chokepoint.

### TC — Token catalog

The catalog (`.figma-sync/ds_catalog.json`) is the file-scope authoritative answer to "what design tokens does this Figma file's variable system define?".

| ID | Invariant | Enforced by |
|---|---|---|
| **TC-1 — Authoritative source.** | Catalog is built from Figma's file-scope variable registry: first `GET /v1/files/{key}/variables/local` when REST scope allows it, otherwise the Figma MCP plugin-runtime export. It is never built from page-walk observation. It enumerates every variable Figma defines for the file, including ones never bound to any node. Page walks may produce *usage* facts; they MUST NOT add a variable to the catalog or set its definitional fields. | `tests/test_token_catalog.py::test_tc1_*` |
| **TC-2 — Complete identity.** | Every variable entry stores: `library_hash`, `collection_id`, `name`, `resolved_type` (`COLOR`/`FLOAT`/`STRING`/`BOOLEAN`), `values_by_mode`, `scopes`, `code_syntax`, `alias_of`, `source` (`figma_api`/`figma_mcp`/`seeded:*`/`observed`). No definitional field is "set later by another code path." | `tests/test_token_catalog.py::test_tc2_*` |
| **TC-3 — No dead fields.** | Every field on `CatalogVariable` and `CatalogLibrary` has exactly one canonical writer. CI fails if a model field is declared but no source location writes it. | source-scan meta-test in `tests/test_dead_fields.py` |
| **TC-4 — Mode-aware storage.** | Variables store `values_by_mode: dict[mode_id, value]`, never flattened to a single value. Readers that need a single value pick an explicit `default_mode_id` from the library entry. There is no implicit last-write-wins. | `tests/test_token_catalog.py::test_tc4_*` |
| **TC-5 — Refresh is page-independent.** | The catalog refresh code path takes `file_key` and a `FigmaClient` only. It never reads or writes any `pages/*.md`, never consults `enriched_hash`, `page_hash`, or `frame_hashes`. It joins `pull` at Tier 1.5 (file-version-gated, once per changed file) — at the same tier as `get_component_sets`. | `tests/test_variables_command.py::test_tc5_*` |
| **TC-6 — Cheap subcommand exists.** | `figmaclaw variables --file-key <key>` refreshes the catalog without touching pages, screenshots, or sidecars. Runtime is dominated by one HTTP call per file. | smoke test |
| **TC-7 — Observability of staleness.** | Each library entry records `source_version` (Figma file version at fetch) and `fetched_at`. Consumers compare the entry's `source_version` against `manifest.files[k].version` to decide if the cache is current. | `tests/test_token_catalog.py::test_tc7_*` |
| **TC-8 — Idempotent writes.** | `save_catalog` skips writes when only `fetched_at` would change (W-1 applied to the catalog). Source-scan meta-test pins this. | `tests/test_catalog_idempotency.py` |
| **TC-9 — Schema upgrades migrate, never drop.** | Schema bumps either migrate forward in place (preserving `seeded:*` entries and any human-set fields) or auto-archive to `ds_catalog.bak.<UTC>.json` and start fresh. Never silently overwrite or warn-and-skip. (LW-2 applied to the catalog.) | `tests/test_catalog_migration.py` |

**`source` enum values:**

- `figma_api` — populated from `/variables/local`. Authoritative.
- `figma_mcp` — populated from Figma MCP plugin-runtime variable export. Authoritative fallback when REST variables scope is unavailable.
- `seeded:css` — imported from a CSS export by `seed_catalog.py` or equivalent. Bridge state when no authoritative variable reader is available.
- `seeded:manual` — hand-added (e.g. `border/width` tokens not present in CSS).
- `observed` — variable ID was seen as a `boundVariables` reference but the definition was never resolved. Legacy / graceful-degradation only; new code MUST NOT produce these.

### TS-S — Token sidecar

Per-page `*.tokens.json` files are the page-scope answer to "what raw or stale token usage exists on this page?".

| ID | Invariant | Enforced by |
|---|---|---|
| **TS-S-1 — Aggregation.** | Sidecar issues are aggregated by `(property, classification, value)` per frame with a `count` field (schema v2). Per-node detail is dropped — the sidecar is consumption-shaped, not observation-shaped. | `tests/test_compact_sidecar.py` |
| **TS-S-2 — Sparse output.** | Only frames with `raw > 0` or `stale > 0` appear in the sidecar. A frame with all-valid bindings is absent. | same |
| **TS-S-3 — Sum preservation.** | Sum of `count` across aggregated entries equals the input issue count — no data loss in aggregation. | same |
| **TS-S-4 — Hex derivation.** | `hex` is derived from `current_value` for color properties only; `None` for numeric. | same |
| **TS-S-5 — `fix_variable_id` survives migration.** | Schema migration (v1 → v2 → vN) preserves human-set or `suggest-tokens`-set `fix_variable_id` values. (LW-2 applied to sidecars.) | `tests/test_sidecar_migration.py` |
| **TS-S-6 — Backfill.** | If a page `.md` exists but its sidecar is missing or schema-stale, the next pull writes it even if page content is unchanged (commit `e100631`, `6a666ac`). | `tests/test_sidecar_backfill.py` |
| **TS-S-7 — Lifecycle.** | Sidecars are pruned alongside their parent `.md` when pages disappear (`prune_utils.py`). | `tests/test_prune_utils.py` |

### REG — File-registry state

File-scope registries are committed cache artifacts. Code and agents must be able to tell the difference between "not checked" and "checked, empty."

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **REG-1** | Explicit registry state | For every tracked file registry, figmaclaw must distinguish three states: not probed, probed-empty, and probed-with-entries. A missing registry artifact is unknown; it is never proof that the upstream registry is empty. An explicit `figmaclaw census --file-key <key>` probe persists probed-empty component state as `_census.md` with `component_set_count: 0`. | Consumer repos use registry files to answer source-of-truth questions. If absence means both "not emitted" and "empty," agents and automation can incorrectly conclude that Figma has no published components or variables. Persisting explicit empty probes keeps high-signal product files quiet while letting important DS files carry a durable "probed empty" fact. | Tap In / LSN had zero published component sets, but missing `_census.md` was ambiguous. Evidence: [PR #129 comment](https://github.com/aviadr1/figmaclaw/pull/129#issuecomment-4343662164), `tests/test_census.py::test_census_reports_empty_registry_for_explicit_file_key`. |

### PP — Pull terminal state

Every manifest page entry is a promise about generated repo state. A pull may skip a page intentionally, but it must not silently remember a page that produced no artifact.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **PP-1** | No silent partial pull | Every non-skipped manifest page entry must end in a terminal data state: either `md_path` is present, or `component_md_paths` is non-empty, or the page is explicitly skipped with reason. The shape `md_path: null` with empty `component_md_paths` is invalid. | This prevents a stable manifest hash from hiding missing markdown forever. Component-only pages are valid, but only when their component markdown paths exist. | PR #129 found 215 pages across `linear-git` stuck in the invalid shape, including 9 design-system pages. Evidence: [PR #129 H2 comment](https://github.com/aviadr1/figmaclaw/pull/129#issuecomment-4340239749), commit [`2c3dc08`](https://github.com/aviadr1/figmaclaw/commit/2c3dc08), `tests/test_doctor.py::test_doctor_surfaces_partial_pull_pages`. |

### NC — Node coverage parity

figmaclaw has multiple walkers over the Figma node tree. They must agree on what counts as renderable input.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **NC-1** | Rendered unit coverage parity | The page parser, renderer, page-hash walker, frame-hash walker, manifest writer, and prune/migration logic must cover the same renderable Figma unit classes: supported `FRAME`, `COMPONENT`, and `COMPONENT_SET` nodes, whether top-level or section-wrapped. | If one subsystem sees a node and another ignores it, figmaclaw can write stale hashes, miss markdown files, or fail to prune generated artifacts. Coverage parity is what makes hash gating and generated output trustworthy. | Top-level `COMPONENT_SET` pages caused partial pulls until parser/hash coverage was aligned. Evidence: commit [`379aa41`](https://github.com/aviadr1/figmaclaw/commit/379aa41), commit [`3cabadc`](https://github.com/aviadr1/figmaclaw/commit/3cabadc), `tests/test_top_level_component_pages.py`, `tests/test_pr_129_adversarial.py`. |
| **NC-2** | Mixed section preservation | A Figma `SECTION` containing both screen frames and component sets must preserve both classes in generated output. One supported child class must not cause another supported child class to disappear. | Figma sections are containers, not exclusive type declarations. Treating a mixed section as only "screens" or only "components" silently drops source data. | Mixed `SECTION` pages dropped component sets until sibling screen/component sections were emitted. Evidence: commit [`33e5c03`](https://github.com/aviadr1/figmaclaw/commit/33e5c03), `tests/test_pr_129_adversarial.py::test_mixed_section_with_frames_and_component_sets_emits_both_outputs`. |

### HSH — Hash coverage

Hash gates are only safe when they cover all source identity that affects generated output.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **HSH-1** | Hash covers rendered identity | Any visible Figma node identity that affects generated markdown must contribute to the relevant page and unit hashes. Invisible nodes stay excluded, and sibling order remains order-insensitive unless order itself is rendered. | Tier 2 and surgical enrichment depend on hashes being a complete summary of rendered source identity. If markdown can change while the hash stays stable, figmaclaw will skip required work. | Adding/renaming variants inside an existing `COMPONENT_SET` originally did not change page hashes or frame hashes. Evidence: commit [`3cabadc`](https://github.com/aviadr1/figmaclaw/commit/3cabadc), [Agent A report H8/H9](https://github.com/aviadr1/figmaclaw/blob/feat/canon-token-architecture-128/docs/pr-129-final-report-agent-A.md), `tests/test_pull_logic.py::test_schema_upgrade_v8_to_v9_picks_up_variant_changes`. |

### SI — Synthetic identity

Synthetic nodes are allowed only when Figma has no natural grouping node for output that figmaclaw must render. Once synthetic identity reaches paths or manifest state, it must be as collision-resistant as real source identity.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **SI-1** | Source-scoped synthetic identity | Synthetic sections, node IDs, and generated paths must encode enough source identity that two different source pages or sections cannot generate the same repo path. | A synthetic ID is not just an in-memory convenience; it becomes persistent manifest and filesystem identity. Generic synthetic IDs create last-writer-wins corruption. | Top-level component pages initially wrote every synthetic component section to `components/ungrouped-components-ungrouped-components.md`. Evidence: commit [`a384b47`](https://github.com/aviadr1/figmaclaw/commit/a384b47), `tests/test_pr_129_adversarial.py::test_synthetic_component_section_path_unique_across_two_real_pages`. |

### MIG — Generated-artifact migration

Generated artifacts are recomputable, but stale generated artifacts still mislead consumers until they are removed or migrated.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **MIG-1** | Own legacy generated names | When figmaclaw changes generated path schemes or schema versions, it must still narrowly recognize, migrate, or prune legacy generated artifacts from previous schemes. | Otherwise old generated files become permanent orphans: committed, stale, and not owned by the current manifest. Generated does not mean harmless once it is in git. | The legacy pre-H6 synthetic file lacked a numeric node suffix, so generated-file detection skipped it. Evidence: commit [`2fcd38b`](https://github.com/aviadr1/figmaclaw/commit/2fcd38b), [Agent A report H10](https://github.com/aviadr1/figmaclaw/blob/feat/canon-token-architecture-128/docs/pr-129-final-report-agent-A.md), `tests/test_pr_129_adversarial.py::test_legacy_ungrouped_components_file_is_generated`. |

### AUTH — Authority claims

Commands must not overstate what their data proves.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **AUTH-1** | Authority claims require authoritative sources | Any command or workflow that claims design-token coverage, or applies irreversible/token-writing decisions, must require catalog entries from authoritative sources (`figma_api` or `figma_mcp`) or explicitly refuse/degrade output. `observed`, `seeded:*`, and `unavailable` entries may support bridge/suggestion workflows only when the output is labeled as non-authoritative. | Observation proves only usage; seeded data is a bridge; unavailable proves absence of access. Treating those as authoritative causes automation to make token decisions from incomplete evidence, while still allowing safe advisory workflows such as seeded suggestions. | PR #129 added `figmaclaw variables --require-authoritative` and workflow gating after "green but unavailable" catalogs were possible. Evidence: [proof-gate comment](https://github.com/aviadr1/figmaclaw/pull/129#issuecomment-4340235267), [MCP authoritative Tap In proof](https://github.com/aviadr1/figmaclaw/pull/129#issuecomment-4340276811), commit [`a1151b9`](https://github.com/aviadr1/figmaclaw/commit/a1151b9). |

### ERR — Failure scoping

Retry suppression is part of the data contract whenever fallback readers exist. The scope of a cached failure must match the scope of the evidence.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **ERR-1** | Persistent and transient failures do not share cache semantics | Retry suppression may be cached across files or runs only for persistent configuration absence, such as missing credentials. Per-file or transient API/MCP failures must remain scoped and retryable for later files/runs. | A transient reader error is not evidence that the reader is unavailable everywhere. Caching it globally silently downgrades unrelated files to `unavailable` and loses authoritative data. | Live `linear-git` CI exposed a transient MCP read-only error that poisoned later files until the cache semantics were split. Evidence: [PR #129 comment](https://github.com/aviadr1/figmaclaw/pull/129#issuecomment-4341220608), commit [`19cd1c0`](https://github.com/aviadr1/figmaclaw/commit/19cd1c0), `tests/test_mcp_variable_export.py`. |

### WF — Workflow recovery

Reusable workflows write generated cache artifacts in shared git branches. Their recovery behavior must preserve the "generated from source" contract.

| ID | Name | Description | Rationale | Proof |
|---|---|---|---|---|
| **WF-1** | Replay deterministic generated artifacts | When a workflow push is rejected for deterministic generated artifacts, recovery must recompute those artifacts from the latest remote source state instead of text-merging stale generated JSON/markdown. | Generated artifacts are cache snapshots. Text-merging two snapshots can create a state that was never generated from any Figma/Linear source. Reset-and-replay preserves determinism and avoids cache corruption. | Concurrent variables/census/enrichment pushes produced generated JSON conflicts. Evidence: [PR #129 replay note](https://github.com/aviadr1/figmaclaw/pull/129#issuecomment-4341938356), commit [`60bfbb4`](https://github.com/aviadr1/figmaclaw/commit/60bfbb4), commit [`f5bdc51`](https://github.com/aviadr1/figmaclaw/commit/f5bdc51), `tests/test_workflow_template_invariants.py`. |

## 5. Design decisions

D1..D10 are carried forward verbatim from `frontmatter-v2-plan.md`. D11..D14 are new and resolve the audit in issue #128.

### D1: Descriptions out of frontmatter

**Decision:** `frames:` stores only node IDs (list), not descriptions (dict). Descriptions live exclusively in the body.

**Why:** Descriptions are LLM prose. Storing them in frontmatter created duplication (frontmatter AND body), sync drift, and double work (`set-frames` + `write-body`). Frontmatter is a machine index — what exists and what changed, not what things look like.

**Tradeoff:** `parse_frame_descriptions()` goes away. Any tool that needs descriptions reads the body tables or calls the LLM. Machines need IDs and hashes; humans and LLMs need prose.

### D2: Per-frame content hashes for surgical enrichment

**Decision:** Compute a content hash per frame (depth-1 children: names, types, text content, component IDs). Store enriched hashes in frontmatter, current hashes in manifest. Diff to find exactly which frames changed.

**Why:** A 500-frame page where 2 frames changed should re-enrich 2 frames, not 500. Without per-frame tracking, any structural change triggers full-page re-enrichment (~$15). With it: 2 screenshots, ~$0.10.

**Why depth-1:** Catches meaningful changes (elements added/removed, text changed, component swapped) while ignoring noise (position shifts, style tweaks). Descriptions rarely become stale from a color change.

### D3: Enrichment state in frontmatter, not manifest

**Decision:** `enriched_hash`, `enriched_at`, `enriched_frame_hashes` live in the `.md` file's frontmatter. Manifest only holds sync cache (`page_hash`, `frame_hashes`).

**Why:**
- **Self-contained**: `inspect` reads one file to check enrichment status. No manifest dependency.
- **No merge conflicts**: concurrent jobs on different pages never conflict. Manifest is a single file — two writers = merge conflict.
- **No single point of failure**: manifest corruption loses cache (recomputable). Enrichment state survives because each page carries its own.
- **Portable**: rename, move, or copy a page to another repo — enrichment state travels with it.
- **Git-friendly**: each page's enrichment history is in its own git blame.

### D4: Manifest is cache, frontmatter is state

**Decision:** Manifest stores only sync engine cache. Frontmatter stores persistent state.

**Manifest (cache, recomputable, lossy):** `page_hash`, `frame_hashes`, `last_refreshed_at`.

**Frontmatter (state, persistent, authoritative):** `frames`, `flows`, `enriched_*`.

If the manifest is deleted, sync re-fetches everything on the next run. **Zero data loss.** If frontmatter is deleted, we lose the page's identity and enrichment history.

D11 extends this axis: file-scope registries (catalog, census) belong in the cache layer because they are recomputable from REST. They do NOT belong in frontmatter.

### D5: `mark-enriched` as separate command

**Decision:** `write-body` writes body only. `mark-enriched` snapshots hashes. Two separate commands, two separate concerns.

**Why:** `write-body` might be used to fix a typo. Coupling it with hash snapshotting would mark a page as fully enriched when it isn't. The enrichment pipeline calls both in sequence; manual edits call only `write-body`.

### D6: Exit codes for errors only

**Decision:** All commands exit 0 on success. Exit 2 for actual errors (not a figmaclaw file, missing manifest, etc.). Business logic status (`needs_enrichment`, `missing_descriptions`) is in the JSON output, never in exit codes.

**Why:** Exit 1 conventionally means error. Using it for "needs enrichment" breaks `set -e` scripts and CI step semantics.

### D7: Frame hash excludes position/size/style

**Decision:** `compute_frame_hash` hashes child names, types, text content, and component references. It ignores absolute position, size, fills, strokes, effects, opacity.

**Why:** Descriptions say "login screen with email input and Sign In button." Moving the button 10px doesn't make that stale. Changing the button text from "Sign In" to "Log In" does.

### D8: Command naming — verbs match semantics

| Command | Verb | Why |
|---|---|---|
| `sync` | sync | Synchronizes structure from Figma to local. |
| `pull` | pull | Pulls all tracked files (git analogy). |
| `census` | (noun) | Snapshot. |
| `variables` | (noun) | Same shape as census — file-scope registry snapshot. |
| `write-body` | write | LLM is authoring prose. |
| `mark-enriched` / `mark-stale` | mark | Sets a flag/state. |
| `inspect` | inspect | Read-only state examination. |
| `set-flows` | set | Writes a specific field value. |
| `screenshots` | (noun) | Downloads artifacts. |
| `suggest-tokens` | suggest | Annotates with non-binding suggestions. |
| `fix-tokens` (future) | fix | Applies suggestions back to Figma. |

### D9: Current hashes in manifest, not frontmatter

**Decision:** Current frame hashes (`frame_hashes`) live in the manifest only. Enriched frame hashes (`enriched_frame_hashes`) live in frontmatter only.

**Why:** Per D4, current is cache, enriched is state. Duplicating in two places bloats large pages (~10KB for 500 frames) and increases git churn.

**Fallback:** If manifest is missing, treat all frames as stale (safe, triggers full re-enrichment).

### D10: Section-level enrichment via per-frame hash aggregation

**Decision:** No per-section hashes or timestamps in frontmatter. Section staleness is computed at runtime by mapping stale frames to sections (body parsing via `parse_sections()`).

**Why:** Per-frame hashes already exist. Computing "which sections are stale" is a join of `manifest.frame_hashes` × `enriched_frame_hashes` × `parse_sections()`. Adding per-section hashes would be redundant aggregation.

**`mark-enriched` remains page-level.** Cannot call after each section — that would mark other still-stale sections as current.

### D11: File-scope cached registries are a peer of page-scope state

**Decision:** Variables, components, styles, and any other file-level Figma data live in dedicated, file-scope cached registries. They refresh at Tier 1.5 of the ladder (once per file when `version` changes), independent of per-page hashing.

**Why:** Variables (and components) change *independently* of any page's content. Before this decision, the catalog was gated on per-page hash invalidation, which meant a Figma file rename of `gray/100` → `gray/100-default` could change the file's `version` without changing any page's hash, and the local catalog would never refresh — even though every consumer of "what tokens does this DS define?" expected an answer. Census already established this pattern for components (commit `13b0146`); D11 generalises it.

**Tradeoff:** A new tier in the refresh ladder. Acceptable — the normal cost is one cheap REST call per changed file (`get_component_sets`, `get_local_variables`), which is well within Tier 1 budget. If REST variables scope is unavailable, `figmaclaw variables --source auto` may use Figma MCP for the catalog refresh instead.

### D12: Library identity is data-derived, never hardcoded

**Decision:** No library hash constants in figmaclaw source. Library identity comes from the file's variable-registry response (`/variables/local` or MCP export), or, for an unknown library, from the catalog's `libraries` map populated by other tracked files.

**Why:** The previous `DS_LIB_HASH = "778120a4..."` and `OLD_LIB_PREFIX = "a3972cba"` constants in `token_scan.py` coupled a general-purpose tool to one customer's setup, in direct violation of the "general-purpose open-source" rule in `CLAUDE.md`. Worse, any unknown library (including a new DS coming online) was silently classified `valid` by the conservative fallback — making the catalog answer "yes, this binding is fine" even when it had no idea what library the binding pointed at.

**How:** `classify_variable_id(var_id, *, libraries)` becomes a pure function over data. The `libraries` argument is the catalog's `libraries` dict. Variables resolving to a known library get the library's name; variables resolving to an unknown library get an explicit `unknown_library` classification (with the lib hash in the issue) — not silent `valid`.

### D13: The catalog stores definitions; sidecars store usage

**Decision:** Two distinct data structures:

- `ds_catalog.json` — **definitions.** Authoritative answer to "what variables exist?". Source: Figma variable registry via REST or MCP. Never observation.
- `*.tokens.json` sidecars — **usage.** Per-page answer to "what bindings and raw values exist on this page?". Source: page walks.

**Why:** The original `ccdd2d7` design collapsed both into observation. The catalog inherited mode-blindness, name-blindness, and library-confusion as a result; see failure modes F2..F5 in §6.

**Implication for `merge_bindings`.** It stops writing definitional fields (`hex`, `numeric_value`, `name`). It records only `observed_on` properties and (new) `usage_count` per `(file_key, variable_id)`. Definitions come exclusively from the variables refresh code path.

### D14: SEEDED entries are first-class via a `source` field

**Decision:** Each catalog variable carries a `source` field with values `figma_api`, `figma_mcp`, `seeded:css`, `seeded:manual`, or `observed`. Advisory readers such as `suggest-tokens` may use seeded bridge entries as labeled, non-authoritative match candidates. Any workflow that claims authoritative coverage or applies token writes must require `figma_api` / `figma_mcp` definitions (AUTH-1); `observed`, `seeded:*`, and `unavailable` are not authoritative.

**Why:** Until every customer team has Enterprise scope `file_variables:read`, CSS-derived seeds remain the only path for many tokens. The `seed_catalog.py` script in linear-git is the precedent (commit `157cd98d6`). Treating `SEEDED:*` IDs as a permanent first-class case (not a temporary workaround) means the schema is honest about its sources, and `fix-tokens` can refuse to apply a `seeded:*` ID until it's resolved to a real Figma variable ID by a future runtime resolution step.

**Tradeoff:** Catalog readers must be tolerant of multiple sources for the same value. `suggest-tokens` flags ambiguity when both a `figma_api` and a `seeded:css` candidate match the same hex, and its output remains advisory unless backed by authoritative definitions.

## 6. Failure-mode catalog

Each row records a failure mode that has actually occurred (or that we shipped a near-miss for) and the invariants that preclude it. Cite by ID in PR review.

| ID | Failure mode | Origin | Precluded by |
|---|---|---|---|
| **F1** | Page-hash gating of file-level data. Variables and other file-scope content change independently of page node trees, but the catalog refresh was inside the per-page loop. Page skips → catalog stale. | Issue #128 audit, Apr 2026. | D11, TC-5 |
| **F2** | Observation-only catalog. Catalog only contained variables figmaclaw happened to encounter as `boundVariables.<prop>.id`. Primitives, unused tokens, alias targets, and variables on un-pulled pages were invisible. | `ccdd2d7` design choice ("zero additional API calls — piggybacks on get_page() data"). Right call for "annotate raw bindings on pull"; wrong call for "tell me what tokens the DS defines." | D13, TC-1 |
| **F3** | Dead model fields. `CatalogVariable.name` shipped in JSON but had no writer. The 22 names on disk were a hand-seeded artifact (`22dcd48f1` in linear-git), not figmaclaw output. | Same audit. | TC-3 |
| **F4** | Mode-blind catalog. `merge_bindings` was last-write-wins on a single value field. Per-mode variables collapsed silently. | Same audit. | TC-4 |
| **F5** | Silent staleness in consumers. `suggest-tokens` reported `no_match` indistinguishably whether the DS doesn't define a token or the catalog hasn't seen it. | Same audit. | TC-7, CR-2 |
| **F6** | Coupled cost — no cheap path. "Get me current token names" required a multi-hour `--force` pull. | Same audit. | TC-6 |
| **F7** | Catalog was neither truth nor cache. Lived in `.figma-sync/` (cache layer per D4) but wasn't recomputable without re-walking every page. | Same audit. | D11, W-1 |
| **F8** | Hardcoded DS library identity in a general-purpose tool. `DS_LIB_HASH` / `OLD_LIB_PREFIX` constants in `token_scan.py:33-34` coupled figmaclaw to one customer's library hashes. Unknown libraries were silently classified `valid`. | Visible in the source from the original `ccdd2d7`. | D12 |
| **F9** | `suggest-tokens` has no terminal application. Tool annotates sidecars with `fix_variable_id` candidates; nothing applies them. Annotations accumulate as disk state and become stale before any consumer acts on them. | Audit confirmed: every sidecar in the linear-git consumer has `suggested_at: null`, no `fix_variable_id` populated. | Future `fix-tokens` RFC; tracked separately. |
| **F10** | Schema migration silently drops user-set data. `e100631`'s sidecar v1 → v2 migration rewrites the file. Once `suggest-tokens` runs, human-set `fix_variable_id` values would be lost on the next migration. | Latent; doesn't bite today because suggest-tokens isn't in CI. | LW-2, TS-S-5 |
| **F11** | Registry absence misread as empty registry. A file with no `_census.md` could be read as "no published components" even when the file had never been explicitly probed or emitted. | Tap In / LSN component audit during PR #129. | REG-1 |
| **F12** | Silent partial pull. A page entry reached stable manifest state with `md_path=null`, `component_md_paths=[]`, and `page_hash=sha256("[]")[:16]`; future pulls skipped it forever. | PR #129 H2; 215 entries in `linear-git`. | PP-1, NC-1, HSH-1 |
| **F13** | Walker coverage mismatch. Parser/renderer/hash logic disagreed about top-level or section-wrapped `COMPONENT` / `COMPONENT_SET` nodes, so some code paths saw real content and others treated the page as empty. | PR #129 H2/H9. | NC-1, HSH-1 |
| **F14** | Mixed section data loss. A `SECTION` containing both frames and component sets rendered one class and silently dropped the other. | PR #129 H11. | NC-2 |
| **F15** | Rendered component variant staleness. Adding or renaming a visible variant inside an existing `COMPONENT_SET` did not change page or frame hashes, so generated variant tables stayed stale. | PR #129 H8/H9. | HSH-1 |
| **F16** | Synthetic path collision. Multiple pages with top-level components generated the same synthetic component-section path; later pages overwrote earlier pages' component markdown. | PR #129 H6. | SI-1 |
| **F17** | Legacy generated orphan. A generated file from an old path scheme was no longer recognized as generated and survived pruning forever. | PR #129 H10. | MIG-1 |
| **F18** | Green but non-authoritative catalog. CI could succeed with `source=unavailable` or observation-only token data while downstream tasks interpreted the catalog as current DS truth. | PR #129 variables proof lane. | AUTH-1, TC-1, TC-7 |
| **F19** | Transient failure poisoned later files. A per-file MCP export error was cached like missing credentials and suppressed authoritative fallback for unrelated later files. | PR #129 live `linear-git` run. | ERR-1 |
| **F20** | Merged generated cache snapshot. Rejected workflow pushes attempted merge-pull recovery for generated JSON, allowing conflict states that were not the output of a deterministic generator over current source. | PR #129 variables workflow lane. | WF-1 |

Each row was either repeated more than once or had a near-miss before being canonized. New rows are appended; nothing is renumbered.

## 7. Document index

Every doc that exists, what it owns, where it points to canon.

| Doc | What it owns now | Relationship to canon |
|---|---|---|
| `skills/figmaclaw-canon/SKILL.md` | This document — invariants, design decisions, refresh ladder, failure modes. Bundled as the `figmaclaw:figmaclaw canon` skill so consumer repos can invoke it without reading figmaclaw's `CLAUDE.md`. | Authoritative. |
| `docs/figmaclaw-canon.md` | Pointer stub at canon's old location, redirecting to the skill above. | Pointer only. |
| `docs/figmaclaw-md-format.md` | Authoritative reference for `.md` file format: frontmatter schema, body structure, single-line flow YAML rule, command-by-command table. | Format spec. References canon for the underlying data contract; canon §1 references it back for format details. |
| `docs/body-preservation-invariants.md` | Detailed test-by-test invariant list for BP, SC, FM, CL classes. | Invariant detail. Canon §4 names the invariants and links here for the test mapping. |
| `docs/body-preservation-design.md` | Historical: why we renamed `enrich`→`sync`, `render_page`→`scaffold_page`. Implementation notes. | Historical. Superseded by canon for current rules. |
| `docs/frontmatter-v2-plan.md` | Original D1..D10 design decisions; per-frame hash design; enrichment flow. | Historical RFC. Decisions D1..D10 are canonized in §5. The rest is implementation notes — keep for archaeology. |
| `docs/sync-observability.md` | `SYNC_OBS` and `SYNC_OBS_PULL` event taxonomy; artifact upload structure. | Operational reference. Canon §3 references it for the refresh ladder's observability hooks. |
| `docs/token-auth-and-rotation.md` | API key handling, secret rotation runbook. | Operational. Canon TC-6 references it for `figmaclaw variables` auth path. |
| `docs/giant-section-strategies.md` | Section-mode enrichment for pages with too many frames for a single LLM dispatch. | Operational. Tied to D10 (section-level enrichment). |
| `docs/failure-postmortem-2026-04-03.md` | Postmortem of CI enrichment cascade failures. | Historical. Lessons-learned doc. |
| `CLAUDE.md` | Developer onboarding, code conventions, ecosystem ownership rule, anti-loop policy summary, commit-discipline rules. | Operational. Anti-loop dim 1..6 are canonized as CR/KS/TS/CW/LW/HE; CLAUDE.md keeps a brief summary and links here. |
| `TODO.md` | Engineering standards (pydantic, basedpyright, ruff, pre-commit, exit codes). | Operational. Standards stay there; invariants live here. |
| `README.md` | User-facing intro and install. | User-facing. Not affected by canon. |
| `INSTALL.md` | Developer install instructions. | Same. |

Consumer-repo fragment (e.g. linear-git's `.agents/AGENTS.md`) cross-references canon §1 (data contract) and §4-TC (catalog invariants) for any agent doing token migration work.

## 8. Anti-pattern checklist for PR review

Use this checklist when reviewing any PR that touches figmaclaw's data model, catalog, sidecars, or enrichment loop. Each item is a question you should be able to answer "yes" to before approving.

### Cross-cutting

- [ ] Does this PR add a loop-break or selector? If yes, is there a cross-run test (CR-1)?
- [ ] Does this PR touch frontmatter fields? If yes, are frame-keyed dicts pruned at the `_build_frontmatter` chokepoint and covered by key-set tests (KS-1)?
- [ ] Does this PR introduce a new LLM row marker? If yes, is the tombstone protocol designed in the same PR (TS-1)?
- [ ] Does this PR walk the body? If yes, does it use `iter_body_frame_rows` / `section_line_ranges` (CW-1)?
- [ ] Does this PR touch a log or schema writer? If yes, does it auto-heal or hard-fail on schema drift (LW-1, LW-2)?
- [ ] Does this PR add a new selection / entry boundary that reads a page `.md`? If yes, is it registered in `_HEALING_ENTRY_POINTS` and does it call `normalize_page_file` (HE-1)?
- [ ] Does this PR add a writer? If yes, does it strip timestamps before comparing existing content (W-1) and round-trip-assert after writing (W-2)?
- [ ] Does this PR add or consume a file-scope registry? If yes, can callers distinguish not-probed, probed-empty, and probed-with-entries (REG-1)?
- [ ] Does this PR change manifest page entries or pull skipping? If yes, can every non-skipped page reach a terminal data state (PP-1)?
- [ ] Does this PR add or change a Figma node walker? If yes, does parser/renderer/hash/manifest/prune coverage stay in parity (NC-1), including mixed sections (NC-2)?
- [ ] Does this PR change generated markdown content? If yes, do page and unit hashes include every visible source identity that can affect that output (HSH-1)?
- [ ] Does this PR create synthetic nodes or paths? If yes, are they source-scoped and collision-proof at the filesystem path layer (SI-1)?
- [ ] Does this PR change generated path schemes or schema versions? If yes, are legacy generated names still narrowly recognized, migrated, or pruned (MIG-1)?
- [ ] Does this PR cache an API/MCP failure or suppress retries? If yes, is the cache scoped only to evidence that is persistent at that scope (ERR-1)?
- [ ] Does this PR recover from rejected pushes of generated artifacts? If yes, does it reset/replay deterministic generation instead of text-merging generated cache snapshots (WF-1)?

### Token catalog and sidecar specifically

- [ ] Does this PR add a field to `CatalogVariable` or `CatalogLibrary`? If yes, is there a writer for it and does the dead-fields meta-test cover it (TC-3)?
- [ ] Does this PR write to the catalog? If yes, is the source field set correctly (`figma_api`, `seeded:*`, never `observed` for new code) (D14, TC-2)?
- [ ] Does this PR change how the catalog is refreshed? If yes, is the refresh page-independent (TC-5)? Does it hit Tier 1.5, not Tier 2?
- [ ] Does this PR touch `classify_variable_id`? If yes, is library identity passed in as data, never read from a hardcoded constant (D12)?
- [ ] Does this PR change the sidecar schema? If yes, does the migration preserve `fix_variable_id` (LW-2, TS-S-5)?
- [ ] Does this PR add a consumer of the catalog? If yes, does it CR-2 staleness-check before producing results?
- [ ] Does this PR claim token coverage or apply token-writing decisions? If yes, does it require authoritative `figma_api` / `figma_mcp` data or explicitly refuse/degrade output (AUTH-1)?

If any answer is "no" or "not sure," the answer is not ready to merge.

---

**Bumping the canon.** New invariants get appended (next ID in the class), never renumbered. New design decisions get the next D-number. Failure modes get the next F-number. This document's IDs are referenced by tests, commit messages, and PR reviews — stability of IDs is itself an invariant.
