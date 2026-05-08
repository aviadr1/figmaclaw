# Migration pipeline — moving a Figma file between design systems

This document explains the *write* side of figmaclaw: the audit-page,
audit-pipeline, and apply-tokens command family, and how they compose
into a deterministic pipeline for evolving a design system.

The rest of figmaclaw is read-shaped — pulling Figma into git so agents
and developers can grep design context. This pipeline is the
counterpart: turning a backlog of "we need to migrate the buttons" or
"we need to apply DS tokens to the marketing pages" into a series of
verifiable, reproducible steps.

## What problem this exists to solve

A typical migration ask: *"move this Figma file from our old design
system to TapIn DS — replace the OLD components with TapIn equivalents,
re-bind colors and spacings to the new tokens, and produce a designer
review pack so Bart can sign off."*

A one-shot agent pass can produce something that *looks* mostly right
at first glance. But Figma files have invisible structure under the
surface: components reference master components in other libraries,
properties inherit from those masters, design tokens propagate through
bindings, and instances can be subtly broken in ways that don't show
up until a designer changes the master and discovers their fix didn't
reach 365 places.

That is not a hypothetical. Round 1 of this work hit exactly that:
**a one-shot pass produced 365 silently-incorrect bindings that took
40,000 tokens of recovery to undo**. The problem isn't that an agent
can't do the migration — it's that an agent can't *prove* the
migration is correct, and there's no second pair of eyes that scales
to thousands of nodes per page.

The migration-pipeline command family turns those passes into:

- a **manifest** of intended changes (versioned, validated, citable),
- **lints** against an accumulated invariant catalog (the FCLAW rule
  namespace) before any write,
- **atomic batched application** through the Plugin API, with chunked
  receipts,
- **REST verification** after each batch that what was supposed to
  land actually landed.

Each stage produces an inspectable artifact. Each stage is
re-runnable and idempotent. Each stage cites stable rule IDs so PRs,
receipts, and incident logs share vocabulary.

## The pipeline shape

```
.figma-sync/ds_catalog.json       (token catalog — authoritative variables)
figma/<slug>/_census.md           (component publication state)
figma/<slug>/pages/*.md           (per-page frontmatter + sections + frames)
figma/<slug>/pages/*.tokens.json  (per-page raw/stale/valid token usage)
        |
        v
+------------------+
| suggest-tokens   |  raw token issues + candidates per node × property
+--------+---------+
         |
         v
+------------------+
| bindings prepare |  resolve candidates × rules × overrides ×
| (port in flight) |  inheritance-preservation → fix-schema manifest
+--------+---------+  (F16 / FCLAW-003 enforced here)
         |
         v
+------------------+
| audit-pipeline   |  invariant lints (FCLAW namespace) → refuse
| lint             |  before write if Tier-1 violations
+--------+---------+
         |
         v
+------------------+    +------------------+
| audit-page       |    | apply-tokens     |
| emit-clone-script|--->| --emit-only |    |
| (creates audit   |    | --execute        |
|  page in Figma)  |    +--------+---------+
+--------+---------+             |
         |                       |  Plugin API writes
         |                       |  through use_figma_exec
         |                       v
         |              +------------------+
         |              | Live Figma audit |
         |              | page (mutations) |
         |              +--------+---------+
         |                       |
         |                       v
         |              +------------------+
         |              | audit-page check |  REST verification:
         |              | audit-page       |  did intent land?
         |              | diagnose         |  any unbound literals?
         +------------->+------------------+  any failed swaps?
                        |
                        v
                 designer review pack
                 (per-decision rows, not per-row decisions)
```

Each box is a real (or in-flight) figmaclaw command. The boxes labeled
*port in flight* are tracked in [issue
#152](https://github.com/aviadr1/figmaclaw/issues/152). The lint
substrate is [issue
#151](https://github.com/aviadr1/figmaclaw/issues/151).

## What's shipped today

| Command | What it does |
|---|---|
| `figmaclaw audit-page fetch-nodes <file_key> <node_id>` | Walk a Figma subtree via REST and emit JSONL records. Foundation for every downstream step. |
| `figmaclaw audit-page build-idmap --src ... --dst ...` | DFS-zip two `nodes.jsonl` files (source + audit clone) into a `source_id → clone_id` map. |
| `figmaclaw audit-page emit-clone-script <file_key> <source_node_id>` | Generate `use_figma`-clean Plugin API JS that clones a page, frame, or section into a new audit page (or merges into an existing one). |
| `figmaclaw audit-page check <file_key> <audit_page_id>` | Compare a binding-intent manifest against what's actually bound on the audit page; emit per-row findings with status taxonomy. |
| `figmaclaw audit-page diagnose <file_key> <audit_page_id>` | Classify unbound literal paints (old-palette / new-palette / unclassified) on the audit page. |
| `figmaclaw audit-pipeline lint --component-map ... --census ...` | Validate the v3 component-migration-map shape and cross-reference each `new_key` against the published `_census.md`. |
| `figmaclaw apply-tokens <fix-manifest> --file ... --page ...` | Take a versioned fix manifest (or legacy compact rows) and apply variable bindings. Three modes: `--dry-run` plans, `--emit-only` writes deterministic batches, `--execute` runs them through the shared MCP executor. |

## What's in flight

- **`bindings prepare`** — resolver that produces the apply-tokens
  fix-schema from raw token issues × rules × overrides × inheritance
  context. Owns F16 enforcement (don't bind clean-inherited instance
  internals). [Issue #152 Tranche A.](https://github.com/aviadr1/figmaclaw/issues/152)
- **`apply-swaps`** — component-swap counterpart to `apply-tokens` for
  cross-DS instance migration (round 2 mature locally; not yet
  upstream). [Issue #152 Tranche B.](https://github.com/aviadr1/figmaclaw/issues/152)
- **Lint framework** — shared rule-IDs, severity tiering, per-rule doc
  pages. [Issue #151.](https://github.com/aviadr1/figmaclaw/issues/151)
- **`audit-page check-swaps`**, **`check-properties`** — verifier
  family extension for component-swap and property-leak detection.
  [Issue #152 Tranche B.](https://github.com/aviadr1/figmaclaw/issues/152)
- **`layout lint`** — generic redundant-wrapper detector (FCLAW-010 /
  F20). Not migration-specific. [Issue #151.](https://github.com/aviadr1/figmaclaw/issues/151)

## The rules — FCLAW namespace

Migrations accumulate invariants the hard way. The FCLAW rule
namespace is the upstream home for those invariants, with three
severity tiers:

| Severity | Behavior | Override | Use case |
|---|---|---|---|
| **error** | Hard-fail; refuses to emit/apply | `--allow-<rule-id>`, recorded in receipts | Tier 1 invariants — physics-of-Figma facts |
| **warn** | Reports but allows | `--no-warn-<rule-id>` | Tier 2 sane defaults |
| **info** | Surfaced when verbose; never blocks | off by default | Tier 3 advisory / cleanup |

Each rule has:

- A stable ID (`FCLAW-NNN`, e.g. `FCLAW-003`)
- A short slug (`fc-clean-inheritance-leak`)
- A doc page at `docs/lint-rules/<id>.md`
- A pure-function Python check that returns `Findings`

### Tier 1 invariants (today's candidates)

Violation = data corruption or silent failure. Refuse by default.

| ID | Rule |
|---|---|
| **FCLAW-001** (`fc-token-name-not-authoritative`) | Refuse a fix referencing a token name that's not in the authoritative catalog (`source ∈ {figma_api, figma_mcp}`). Caught when an agent invented `radius-5xl=20` mid-pass and the binding silently pointed at nothing. |
| **FCLAW-002** (`fc-variable-source-non-authoritative`) | Refuse a fix targeting a variable whose catalog source is `seeded:legacy` / `observed`. These don't have stable cross-file identity. |
| **FCLAW-003** (`fc-clean-inheritance-leak`) | Refuse a fix targeting a property on an INSTANCE descendant where the property cleanly inherits from the master. Detaching a clean-inherited instance breaks DS propagation. **The single most consequential rule** — round 1 hit it on 365 rows. |
| **FCLAW-004** (`fc-atomic-limit-exceeded`) | Refuse a `use_figma` call exceeding the 50KB Plugin API limit; chunk first. |
| **FCLAW-009** (`fc-old-component-must-have-target-decision`) | Every OLD-DS instance must have an explicit migration-map decision (`replace_with_new_component` / `compose_from_primitives` / `designer_audit_required` / `out_of_scope`). Round 2 surfaced this when 20 mobile components were silently dropped from the audit. |

### Tier 2 sane defaults

| ID | Rule |
|---|---|
| **FCLAW-005** (`fc-token-name-ambiguous`) | Warn when a token name has multiple candidate variables in the catalog. |
| **FCLAW-006** (`fc-suggest-tokens-cross-library`) | Warn if `suggest-tokens` output isn't filtered to the migration target. |
| **FCLAW-007** (`fc-radius-round-up`) | Round each `cornerRadius` to the next-higher published radius token. |
| **FCLAW-008** (`fc-spacing-round-up-or-overflow`) | Round each `spacing` / `padding` to the next-higher published spacing token. If the raw value exceeds the highest token, leave it raw and emit a designer-audit row. |

### Tier 3 advisory

| ID | Rule |
|---|---|
| **FCLAW-010** (`fc-redundant-single-child-wrapper`) | Auto-layout frame with one child, no styling, no padding, no independent layout behavior → flag as candidate for flattening. **Not migration-specific** — every Figma file accumulates these. |

The full incident archive that produced these rules lives in the
gigaverse migration repo at
`figma_migrations/login-sign-up-onboarding-2026-04-29/audit-log.md`
(F0–F20). Other consumers can build their own per-org incident
archive that maps to the same FCLAW namespace.

## What stays in your repo vs what figmaclaw owns

| Layer | Owner | Why |
|---|---|---|
| Per-migration data files (`bindings_for_figma.json`, `idmap.json`, receipts, audit pages) | Your repo | Run-specific state. Git-tracked for incident archaeology. |
| Migration policy (`bindings.md` rules / overrides / pending_designer_review, `component_migration_map.json`) | Your repo | Designer-touchable. Gigaverse has theirs; yours are different. |
| Per-org incident archive (your `audit-log.md` / `friction-catalog.md`) | Your repo | The discovery context behind invariants. F-numbers are your team's. |
| Rule namespace (FCLAW-NNN), rule check functions, doc pages | figmaclaw | Stable IDs across consumers. The check is shared; the violations are yours. |
| Catalog (`ds_catalog.json`), census (`_census.md`), page mirrors | figmaclaw (via `pull` / `variables` / `census`) | Single source of truth for "what does the DS publish?" |
| Apply executor (`use_figma_exec`), MCP client (`FigmaMcpClient`) | figmaclaw | Plumbing. No team should rebuild this. |
| Designer-pack format (Slack / Excel / Notion / etc.) | Your repo | Org-specific. figmaclaw provides screenshots and per-row receipts; you assemble. |

The boundary: **figmaclaw owns shape; your repo owns content**.
figmaclaw provides the schemas, the lints, the executor, the
verifiers. Your repo provides the rules data, the migration map data,
the receipts/journals.

## A walkthrough on a tiny example

Pick a 1-frame source page (the smallest meaningful migration). The
following commands run end-to-end against a real Figma file. None
exceed `--dry-run` until step 7.

```bash
# Setup
export FIGMA_API_KEY=...
FILE=rvBhmhkDGFiZe6cDnG6SGU
SRC=8163:5295  # Mobile App / Email / Default view (1 frame)

# 1. Refresh DS state — single source of truth for downstream steps
figmaclaw pull --file-key "$FILE"
figmaclaw variables --file-key "$DS_FILE_KEY"
figmaclaw census

# 2. Snapshot the source subtree
figmaclaw audit-page fetch-nodes "$FILE" "$SRC" --out nodes.jsonl

# 3. Annotate token candidates (writes the page sidecar)
figmaclaw suggest-tokens \
    --sidecar figma/<slug>/pages/<page>.tokens.json \
    --library tap --library lsn

# 4. Lint the migration map *before* any write
figmaclaw audit-pipeline lint \
    --component-map component_migration_map.v3.json \
    --census figma/tap-in-design-system-<key>/_census.md

# 5. Clone source onto a new audit page
figmaclaw audit-page emit-clone-script "$FILE" "$SRC" \
    --title "🛠 Audit — Email Default" \
    --out clone.use_figma.js
# (run clone.use_figma.js via use_figma — creates the audit page;
#  read result.targetPageId)
AUDIT_PAGE=...

# 6. Snapshot the clone, build a real idmap
figmaclaw audit-page fetch-nodes "$FILE" "$AUDIT_PAGE" --out audit_nodes.jsonl
figmaclaw audit-page build-idmap \
    --src nodes.jsonl --dst audit_nodes.jsonl --out idmap.json --strict

# 7. Apply token bindings (dry-run first; --execute when satisfied)
figmaclaw apply-tokens bindings_for_figma.json \
    --file "$FILE" --page "$AUDIT_PAGE" --dry-run
figmaclaw apply-tokens bindings_for_figma.json \
    --file "$FILE" --page "$AUDIT_PAGE" --batch-dir batches/ --execute

# 8. Verify
figmaclaw audit-page check "$FILE" "$AUDIT_PAGE" \
    --manifest bindings_for_figma.json --idmap idmap.json
figmaclaw audit-page diagnose "$FILE" "$AUDIT_PAGE" \
    --old-palette palettes/old.json --new-palette palettes/new.json
```

After step 8, the audit page shows what landed; the verifier reports
quantify it; failures iterate via `--remaining-out` re-runs of step
7. When the page looks right, assemble the designer-review pack
(figmaclaw provides screenshots; you provide the format).

## Designer feedback loop

The designer-review pack is **per-decision, not per-row**. A migration
that touched 2,000 nodes should not produce a 2,000-row spreadsheet.
It should produce ~20 rows, each capturing a *category* the designer
needs to decide on — "12 instances of `#0F2738` map to `fg/default` —
confirm or change", "the `#29B95F` success badge has no exact TapIn
match — request a new token or accept the closest", "Toast
notifications has no published TapIn equivalent — known_gap or compose
from primitives".

The lints + verifiers compress per-row noise into per-decision rows
because they classify findings into:

- **Tier 1 violations** — must be 0 (hard-fail before getting here)
- **Known-safe deferrals** — Figma Widget opaqueness, F16-protected
  inheritance gaps, designer-decided out-of-scope items
- **Designer-review categories** — the spreadsheet rows
- **Unknown** — must be 0 by closeout, otherwise the migration ships
  with unverified state

Without the lint / verifier substrate, every row is potentially a
designer question and the review pack collapses under its own weight.

## When *not* to use this pipeline

- **Single-file design tweaks** that don't cross a DS boundary —
  overkill. Just edit in Figma.
- **Pure read-side work** — pulling Figma into markdown for agent
  context is what `pull` / `inspect` / `screenshots` are for. The
  apply path adds nothing.
- **Brand-new design files where no DS exists yet** — the catalog +
  census are empty, so lints have nothing to enforce. Build the DS
  first; migrate to it later.

The pipeline pays back when (a) the work is repetitive enough that a
one-shot pass would produce subtle correctness failures, and (b) the
target DS is published / authoritative enough that the catalog has
real content to lint against. Below either threshold, the cost of the
machinery exceeds its value.

## Further reading

- **Lint framework** — [issue #151](https://github.com/aviadr1/figmaclaw/issues/151) (the rule-namespace + severity-tiering proposal)
- **Port roadmap** — [issue #152](https://github.com/aviadr1/figmaclaw/issues/152) (which local migration scripts have moved upstream + which are next)
- **Apply-tokens design** — [issue #42](https://github.com/aviadr1/figmaclaw/issues/42) and `figmaclaw/apply_tokens.py`
- **Audit-page primitives** — PRs [#147](https://github.com/aviadr1/figmaclaw/pull/147) and [#148](https://github.com/aviadr1/figmaclaw/pull/148)
- **Per-org incident archive (gigaverse)** — `figma_migrations/login-sign-up-onboarding-2026-04-29/audit-log.md` in the linear-git repo (F0–F20)
- **Friction catalog (gigaverse, run-specific)** — `figma_migrations/sprint16-registration-onboarding-2026-05-07/friction-catalog.md` in linear-git
