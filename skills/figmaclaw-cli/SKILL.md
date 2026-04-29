---
name: figmaclaw CLI
description: Use when invoking figmaclaw commands from a consumer repo (linear-git or similar) — pulling Figma data, enriching pages, snapshotting component sets, refreshing the design-token catalog, or running token suggestions. Covers each command's purpose, refresh tier, and which artifact it writes. Companion to the `figmaclaw:figmaclaw canon` skill, which holds the underlying data contract and invariants.
---

# figmaclaw CLI reference

A consumer-side guide to figmaclaw commands. For the underlying data contract, invariants, and the design rationale behind each command, invoke the **`figmaclaw:figmaclaw canon`** skill — that's authoritative. This skill is the practical "which command writes which artifact" lookup.

## Refresh-trigger summary

figmaclaw data lives in four tiers (per canon §3). Pick the cheapest tier that answers your question.

| Tier | Trigger | Cost | Refreshed by |
|---|---|---|---|
| File meta | every `pull` (1 cheap REST call/file) | <1s | `pull` |
| File-scope registries (variables, components) | when file `version` changes | 1 REST call/file | `variables`, `census`, also during `pull` |
| Page structure | when file version changed AND page hash differs | 1 REST call/page | `pull`, `sync` |
| Page body (LLM prose) | when frame hash differs from `enriched_frame_hashes` | LLM $$ | `claude-run` → `write-body` |

If you only need updated tokens, run `figmaclaw variables --file-key <k>` — don't run `pull --force`.

## Commands and what they write

### Pulling structure

| Command | What it does | Writes |
|---|---|---|
| `figmaclaw pull` | Bulk sync all tracked files. Skips at file-version layer; per-page hash gating beneath. Auto-fetches component_sets and (when implemented) variables for changed files. | `figma/{slug}/pages/*.md` (frontmatter), `figma/{slug}/components/*.md` (frontmatter), `*.tokens.json` sidecars, `.figma-sync/manifest.json`, `.figma-sync/ds_catalog.json` |
| `figmaclaw pull --file-key <k>` | Same, single file. | Same, scoped. |
| `figmaclaw pull --force` | Bypasses page-hash skip; rewrites every page. **Slow.** Use only when schema bumps or for explicit re-render. | Same. |
| `figmaclaw sync <md_file>` | Re-sync one page from REST, preserving body. | One `.md` (frontmatter only) + manifest. |
| `figmaclaw track <file_key>` | Register a Figma file for syncing. | `.figma-sync/manifest.json`. |
| `figmaclaw list <team_id>` | Discover Figma files in a team. | stdout (or `--track-only` to add to manifest). |

### File-scope registries

| Command | What it does | Writes |
|---|---|---|
| `figmaclaw census` | Snapshot published component sets to `_census.md` per tracked file. Hash-gated; no commit if registry unchanged. | `figma/{slug}/_census.md`. |
| `figmaclaw variables` | Refresh DS variable catalog from Figma's variable registry. Default `--source auto` tries REST `/variables/local`, then Figma MCP plugin-runtime export when REST lacks `file_variables:read`. Per canon TC-1, these are authoritative sources for token names, modes, scopes. | `.figma-sync/ds_catalog.json`. |
| `figmaclaw variables --file-key <k>` | Same, single file. Add `--source rest` to disable MCP fallback, or `--source mcp` to force plugin-runtime export. | Same, scoped. |

### Inspection (read-only)

| Command | What it does |
|---|---|
| `figmaclaw inspect <md_file>` | Show page state: frame count, sections, enrichment status. |
| `figmaclaw inspect <md_file> --json` | Machine-readable. Includes `needs_enrichment`, `pending_frames`, `stale_frames`. |
| `figmaclaw inspect <md_file> --needs-enrichment` | Boolean shortcut for CI gating. |
| `figmaclaw doctor` | Verify install, env vars, manifest sanity. |
| `figmaclaw self skill <name>` | Print a bundled skill's content. |

### LLM enrichment workflow

The body of a page `.md` is LLM-authored. The flow is gated by frame hashes so unchanged frames are not re-described.

```bash
figmaclaw inspect <md_file> --json                # check needs_enrichment, list stale frames
figmaclaw screenshots <md_file> --stale           # download PNGs for new + modified frames
# LLM reads PNGs, writes new body content
figmaclaw write-body <md_file> < new-body.md      # write body, preserves frontmatter (BP-6)
figmaclaw set-flows <md_file> --flows '[[...]]'   # write inferred flows to frontmatter
figmaclaw mark-enriched <md_file>                 # snapshot hashes (D5)
```

For per-section enrichment on giant pages (>80 frames), use `--section <node_id>` on `screenshots` and `write-body`. See the `figmaclaw:figma-enrich-page` skill for the full LLM prompt and orchestration.

### Token migration

| Command | What it does | Writes |
|---|---|---|
| `figmaclaw suggest-tokens --sidecar <path>` | Annotate raw/stale token issues with DS-variable candidates. | A sibling `<base>.suggestions.json` next to the sidecar (sidecar itself is read-only — never mutated). |
| `figmaclaw suggest-tokens --sidecar <path> --library tap --library lsn` | Limit candidates to specific DS libraries (substring match on library name OR `library_hash`). | Same sibling file. **Use this for migration audits** — without `--library`, suggestions can point at the OLD design system instead of the migration target. |
| `figmaclaw suggest-tokens --sidecar <path> --output <path>` | Write to a custom path. | `<path>`. |
| `figmaclaw suggest-tokens --sidecar <path> --output -` | Write JSON to stdout (informational messages go to stderr — pipeable to `jq` etc). | stdout. |
| `figmaclaw suggest-tokens --sidecar <path> --dry-run` | Print stats without writing anywhere. | nothing. |

`suggest-tokens` reads `.figma-sync/ds_catalog.json`. If the catalog is stale, run `figmaclaw variables` first; per canon CR-2 the consumer should refuse to produce results from a stale catalog.

The output file (`*.suggestions.json`) is regeneratable and should be added to `.gitignore` — it's recomputed from the sidecar + catalog on every run, so checking it in causes merge conflicts and quietly stale data.

### Webhooks (server side)

| Command | What it does |
|---|---|
| `figmaclaw webhooks list` | List registered Figma file-modified webhooks for a team. |
| `figmaclaw webhooks ensure <file_key> --target <url>` | Idempotently create/update a webhook. |
| `figmaclaw webhooks delete <id>` | Remove a webhook. |
| `figmaclaw apply-webhook <payload.json>` | Process a Figma webhook payload locally (incremental sync). |

### CI scaffolding

| Command | What it does |
|---|---|
| `figmaclaw init` | Copy reusable workflow templates into `.github/workflows/`. |
| `figmaclaw init --with-webhook-proxy` | Also scaffold a Cloudflare Worker proxy for Figma webhooks. |
| `figmaclaw workflows status` | Compare local workflow files against bundled templates; flag drift. |
| `figmaclaw claude-run <target>` | Discover and enrich files via Claude Code CLI (used inside CI). |
| `figmaclaw stream-format` | Reformat `claude -p`'s stream-json output for human-readable CI logs. |

## Common pitfalls

- **Don't edit `figma/**/*.md` by hand**, especially the frontmatter. The body is LLM-authored; for changes, use `write-body` with the body content, never a partial edit. (Canon BP-1..6.)
- **Don't run `pull --force` to refresh the catalog.** Use `figmaclaw variables` instead — it's seconds, not hours. (Canon TC-5, TC-6.)
- **Don't add hardcoded library hashes** in any code or skill that classifies tokens. Library identity comes from `ds_catalog.json` `libraries` map, populated by `figmaclaw variables`. (Canon D12.)
- **Single-line YAML flow style** for `frames`, `flows`, `enriched_frame_hashes`, `component_set_keys`, `raw_frames`. The renderer enforces this; never block-indent these manually.

## Refer to the canon

For:
- The four-layer data contract (frontmatter / body / manifest / file-scope registries)
- All invariant classes (BP, SC, FM, CL, W, CR, KS, TS, CW, LW, HE, TC, TS-S)
- All design decisions (D1..D14)
- The full failure-mode catalog (F1..F10)

→ Invoke `figmaclaw:figmaclaw canon`.
