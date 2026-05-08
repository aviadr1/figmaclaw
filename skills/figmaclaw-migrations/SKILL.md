---
name: figmaclaw migrations
description: Use when an agent task involves moving Figma content from one design system to another, applying token bindings, verifying that a migration landed, or extending the FCLAW invariant taxonomy. Covers the audit-page / audit-pipeline / apply-tokens command family, the FCLAW rule namespace, and the shim-then-port pattern for migration-folder scripts.
---

# figmaclaw migrations skill

Use this skill when the task involves:

- moving Figma content from one design system to another (e.g., OLD → TapIn)
- applying design-token bindings to a Figma file at scale
- verifying that a migration's intended changes actually landed
- detecting drift between an audit clone and a source page
- extending the lint catalog (the **FCLAW** rule namespace) with a new invariant
- porting a local migration script upstream to figmaclaw

For the design rationale, the doctrine of "manifests are the
contract", and the full pipeline shape, read
[docs/migration-pipeline.md](../../docs/migration-pipeline.md). This
skill is the orientation layer — the "where am I in the pipeline"
landmarks and the don't-do list.

## The pipeline at a glance

```
suggest-tokens → bindings prepare → audit-pipeline lint
                                          │
                       audit-page emit-clone-script
                                          │
                                 apply-tokens (or apply-swaps)
                                          │
                       audit-page check / diagnose / check-swaps
```

Each stage produces an inspectable artifact. Each stage is
re-runnable and idempotent. Skip a stage and you lose either rule
enforcement (skip lint), apply-time atomicity (skip emit-clone-script
+ a clean audit page), or post-write verification (skip the check
family).

## When the pipeline is the right tool

- The work touches **>~50 nodes** of the same kind. Below that, manual edits in Figma are faster.
- You have a **published design system** (a catalog with `figma_api` / `figma_mcp`-source variables). Without one, lints have nothing to enforce.
- You can produce a **migration map** (`component_migration_map.v3.json`) that names the OLD → NEW component pairs, OR you have a `bindings.md` policy file with rules / overrides / pending_designer_review entries.
- You're prepared to do a **dry run + designer review** before flipping `--execute`. The pipeline is built for that loop; bypassing it removes most of the value.

If the task is a single-file design tweak, or a brand-new file with no
target DS, this skill doesn't apply.

## The FCLAW invariant catalog

When the lint framework (issue #151) lands, rules will be cited by
stable IDs:

| ID | Slug | Severity | What it checks |
|---|---|---|---|
| FCLAW-001 | `fc-token-name-not-authoritative` | Tier 1 error | Token name resolves to an authoritative catalog entry |
| FCLAW-002 | `fc-variable-source-non-authoritative` | Tier 1 error | Variable's `source` is `figma_api` or `figma_mcp` |
| FCLAW-003 | `fc-clean-inheritance-leak` | Tier 1 error | No fix targets a clean-inherited INSTANCE descendant property |
| FCLAW-004 | `fc-atomic-limit-exceeded` | Tier 1 error | No emitted `use_figma` call exceeds 50 KB |
| FCLAW-005 | `fc-token-name-ambiguous` | Tier 2 warn | Token name has only one catalog candidate |
| FCLAW-006 | `fc-suggest-tokens-cross-library` | Tier 2 warn | suggest-tokens is filtered to migration target |
| FCLAW-007 | `fc-radius-round-up` | Tier 2 default | radius rounds up to next-higher published token |
| FCLAW-008 | `fc-spacing-round-up-or-overflow` | Tier 2 default + Tier 3 advisory | spacing rounds up; overflow emits designer-audit row |
| FCLAW-009 | `fc-old-component-must-have-target-decision` | Tier 1 error | Every OLD-DS instance has an explicit map decision |
| FCLAW-010 | `fc-redundant-single-child-wrapper` | Tier 3 advisory | No styling / padding / sizing wrapper around a single child |

Cite rule IDs in PR descriptions, commit messages, and refusal
receipts. The shape mirrors ESLint / Pyright / ruff so violations are
discoverable, citable, and overrideable with audit trail.

The **gigaverse-specific incident archive** (F0–F20) that produced
these rules lives in the linear-git repo at
`figma_migrations/login-sign-up-onboarding-2026-04-29/audit-log.md`.
Other consumer orgs build their own incident archives mapping to the
same FCLAW namespace.

## The shim-then-port pattern

The migration repo (linear-git) has accumulated ~30 local scripts
across 3 rounds. They've been ported upstream in waves:

1. **Local script proves the algorithm** on real migrations (round 1, round 2 minimum — three independent runs gives high confidence).
2. **Once shape is stable, port upstream** as a `figmaclaw <command>` (PRs #147, #148, #150 are examples).
3. **Replace the local script with a 15-line shim** that translates the legacy CLI to the new upstream invocation. Migration repos depending on it keep working with no caller-side changes.

This is why `figma_migrations/_figmaclaw_shims.py` (in linear-git)
exists. When deciding whether to port a local script:

- **Has it run unchanged across ≥2 migrations?** If yes, port candidate.
- **Is it migration-folder-state-shaped?** (e.g., specific to one organization's designer-pack format) — stays local.
- **Is the shape still being discovered?** — stay local; port after stability.

The current port roadmap is [issue
#152](https://github.com/aviadr1/figmaclaw/issues/152).

## Instance/master override inspection

Use `figmaclaw inspect-instance --file-key "$FILE" --node "$INSTANCE" --current-ds-hash "$TARGET_COMPONENT_SET_KEY"` to audit one INSTANCE after a component swap. For migration pages, prefer batch mode: `--nodes-from audit_nodes.jsonl --filter type=INSTANCE`; the CLI chunks REST reads and skips synthesized nested instance ids containing `;`.

- `override_properties != []` is the structural Rule B signal: the instance points at the target master, but still carries detached overrides, so the writer missed `resetOverrides()`.
- `master.is_current_ds == false` is the complementary Rule A signal when published identity is available: the instance still points at a legacy master.
- `master.published_key` prefers the component-set key over the component key. `--current-ds-hash` accepts that component-set key, a component key, a library hash, or the target DS file key.

## Don't-do list

- **Don't** propose a new FCLAW rule without ≥2 migration uses to validate the shape. The lint catalog is small and high-quality on purpose.
- **Don't** bypass FCLAW-003 (clean-inheritance-leak) without writing up the rationale in the migration retro. This is the rule that cost 365 silent corruptions to learn.
- **Don't** invent token names not in the catalog (FCLAW-001). Run `figmaclaw audit-pipeline lint` before any apply.
- **Don't** conflate token bindings (`apply-tokens`) with component swaps (`apply-swaps`, in flight). They share infrastructure but solve different shapes — token rebinding is per-property; swaps mutate node identity.
- **Don't** apply against a source page directly. Always clone with `audit-page emit-clone-script` first; the source is the reference, the audit page is the experiment.
- **Don't** skip dry-run on the first apply-tokens invocation of a migration. Refusal counts in dry-run mode tell you whether the upstream-of-apply pipeline produced a clean fix-schema.
- **Don't** trust a migration as "done" without `audit-page check` (verifies bindings landed) AND `audit-page diagnose` (verifies no unbound DS-color literals remain). One verifier is not enough.

## Related skills

- **`figmaclaw canon`** — the data contract under all of this (manifest layout, page schema, ds_catalog shape). Authoritative for "is this change safe to the data model?".
- **`figmaclaw CLI`** — practical command reference; "which command writes which artifact". Companion to this skill.
- **`figma-mcp-tools`** — for read operations during pre-migration discovery (component lookup, token inspection, variable queries).
- **`figma-enrich-page`** — orthogonal; for refreshing page-level prose when the source file structure changes.

## Quick command reference

```bash
# 0. Refresh DS state
figmaclaw doctor
figmaclaw pull --file-key "$FILE"
figmaclaw variables --file-key "$DS_FILE"
figmaclaw census

# 1. Snapshot source
figmaclaw audit-page fetch-nodes "$FILE" "$SRC" --out nodes.jsonl

# 2. Lint migration map
figmaclaw audit-pipeline lint \
    --component-map component_migration_map.v3.json \
    --census figma/<ds-slug>/_census.md

# 3. Clone source onto audit page (uses use_figma)
figmaclaw audit-page emit-clone-script "$FILE" "$SRC" \
    --title "🛠 Audit — <run name>" --out clone.use_figma.js

# 4. Build idmap + apply
figmaclaw audit-page fetch-nodes "$FILE" "$AUDIT_PAGE" --out audit_nodes.jsonl
figmaclaw audit-page build-idmap \
    --src nodes.jsonl --dst audit_nodes.jsonl --out idmap.json --strict

figmaclaw apply-tokens fixes.json \
    --file "$FILE" --page "$AUDIT_PAGE" --dry-run
figmaclaw apply-tokens fixes.json \
    --file "$FILE" --page "$AUDIT_PAGE" --batch-dir batches/ --execute

# 5. Verify
figmaclaw audit-page check "$FILE" "$AUDIT_PAGE" \
    --manifest fixes.json --idmap idmap.json
figmaclaw audit-page diagnose "$FILE" "$AUDIT_PAGE" \
    --old-palette palettes/old.json --new-palette palettes/new.json

# Batch swap audit
figmaclaw inspect-instance \
    --file-key "$FILE" \
    --nodes-from audit_nodes.jsonl \
    --filter type=INSTANCE \
    --current-ds-hash "$TARGET_COMPONENT_SET_KEY"
```

For the full walkthrough with explanations and rationale, see
[docs/migration-pipeline.md](../../docs/migration-pipeline.md).
