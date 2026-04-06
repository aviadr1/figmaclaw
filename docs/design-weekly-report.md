# Design Weekly Report — Design Doc

**Status:** In progress (diff tool complete, weekly report next)
**Owner:** Aviad (with Claude)
**Last updated:** 2026-04-06

## Goal

Generate a **weekly design report** that captures what designers actually changed in the company's Figma files over the past week, combined with the design team's Linear activity. The report should be:

1. **Accurate** — show real designer work, not bot noise (enrichment, schema migrations, initial tracking).
2. **Visual** — include Figma screenshots inline, not just text.
3. **Structured** — Mermaid flow diagrams where available, cross-references between Linear issues and Figma pages.
4. **Automatable** — generated on a schedule (e.g. every Friday) and posted to a GitHub Discussion or Slack.

This is the design equivalent of the existing `friday-report` skill that produces an engineering weekly report from Linear activity.

## Non-goals

- Not trying to replace the engineering `friday-report` — this is a parallel, design-focused report.
- Not trying to surface every edit a designer made — only **structural** changes (frames added/removed/renamed, new pages, flow changes). Small within-frame edits (colors, text tweaks) are out of scope.
- Not trying to auto-enrich based on diffs (that's a separate follow-up — see TODOs).

## Motivation

The existing engineering weekly report is sourced from `linear/teams/*/issues/` git history. For the **DSG (design)** team, this captures what tickets changed status — but most design work doesn't map 1:1 to Linear tickets. Designers iterate in Figma continuously and sync to Linear after major milestones. The **real** weekly design activity lives in Figma itself.

Initial attempts to detect Figma activity from git history of the tracked `.md` files failed because:

- The `.md` files only started being tracked on 2026-03-31 (the figmaclaw repo deployment), so there is no git history from before that date.
- Early commits conflate bulk initial sync, schema migrations, and real incremental updates.
- The enrichment bot generates hundreds of commits per week that look like "changes" but are just adding descriptions.

**Key insight from user:** *"figuring out what happened last week is very impossible because we're finishing enriching the .md files only now"* — the right source of truth is the Figma API itself, which has full version history per file.

## Architecture

```
figmaclaw diff figma/ --since 7d --format json
  │
  ├─ Phase 0: get_file_meta(depth=1) for all 52 files
  │   → skip files where lastModified < cutoff (~30 skipped)
  │
  ├─ Phase 1: get_versions() for ~20 candidates
  │   → find files with ≥1 version in window (~4 active)
  │
  └─ Phase 2: get_file_shallow(depth=3) × 2 per active file
      → current tree + old-version tree
      → compare frame lists per page → diff output
```

**Total runtime: ~2:18 for 52 tracked files** (down from "hangs forever").

## What's done

### 1. `figmaclaw diff` — CLI command ✅

**Committed:** `c1bcb1d` (initial), `684a3ed` (perf fixes + bug fixes)

```bash
figmaclaw diff [TARGET] --since 7d [--format text|json] [--progress]
```

### 2. Performance optimizations ✅ (committed in `684a3ed`)

| Optimization | Before | After |
|---|---|---|
| `get_file_shallow(depth=3)` instead of `get_file_full` | 50+ MB per file, 3.6 GB RAM, hangs | ~KB per file, completes fast |
| `get_file_meta` pre-filter before version lookup | 52 version lookups (~4 min) | ~16-21 lookups (~1 min) |
| Rate limit 14 → 30 RPM | ~4.3s between requests | ~2s between requests |
| Batch page IDs with 400 fallback | URL limit errors | Batches of 10 |
| Per-file 5-min timeout | Hangs forever | Fails cleanly |
| httpx timeout 120s → 300s | Timeouts on large responses | Completes |

### 3. Bug fixes ✅ (committed in `684a3ed`)

- **VersionSummary nullable fields:** Figma returns `null` for `label`/`description` on autosave versions. Pydantic rejected these, causing `FigmaAPIValidationError` silently swallowed by `asyncio.gather`. All 52 files silently failed. Fixed by making fields `str | None`.
- **Silent error dropping:** Version lookup failures now reported in progress output.
- **Connection retry:** `_get` and `_get_url` retry on connection errors with exponential backoff.

### 4. Verified results ✅

Running `figmaclaw diff figma/ --since 7d` on 2026-04-06, all 4 active files complete:

| File | Versions | Pages changed | Summary |
|---|---|---|---|
| **branding** | 45 (Bartosz, Figma) | 6 pages | round 2: +21, round 3: +64/-2, round 4: NEW (44), round 5: NEW (15) |
| **mobile-app** | 18 (Bartosz, Figma) | 4 pages | Live UI: +13, Community: +1, stage widgets: +39/-1, stage improvements: NEW (10) |
| **web-app** | 10 (Bartosz, Abhishek, Figma) | 2 pages | claude test: NEW (18), Community: +2 |
| **untitled-ui** | 2 | 0 | edits only (no structural changes) |

**mobile-app mystery resolved:** The earlier "zero structural changes" was caused by the nullable fields bug — all files were silently failing validation, not just mobile-app. With the fix, mobile-app shows real changes.

### 5. `figmaclaw image-urls` — CLI command ✅

Gets Figma render URLs for specific frames without downloading. Used for embedding screenshots in reports.

```bash
figmaclaw image-urls path/to/page.md --nodes 11:1,11:2 --scale 0.5
```

### 6. Tests ✅

16 tests passing including:
- 3 new tests for nullable fields and null-label version parsing
- All integration tests updated for `get_file_shallow` + `get_file_meta` mocks

## TODOs

### P0 — build the design weekly report generator

- [ ] **Create `.claude/skills/design-weekly-report.md`** — a skill that:
  1. Runs `figmaclaw diff figma/ --since 7d --format json` to get Figma activity.
  2. Reads DSG team activity from `linear/teams/DSG/issues/` via git log.
  3. Cross-references DSG issues to Figma pages by file_key / page_node_id matching.
  4. For each page with changes, calls `figmaclaw image-urls` to get screenshot URLs.
  5. Renders a markdown report with screenshots, tables, timeline, and cross-references.
  6. Posts to a GitHub Discussion or Slack.

- [ ] **Create `.github/workflows/weekly-report-design.yaml`** — CI automation modeled on the existing `weekly-report.yaml`.
- [ ] **Create `.github/design-report-prompt.md`** — the CI entrypoint prompt for Claude.

### P1 — remaining diff improvements

- [ ] **Handle page deletions** — add `is_removed_page` field to PageDiff and report pages that existed in old version but not new.
- [ ] **Filter autosave versions** — collapse 'Figma' autosaves or drop them from counts.
- [ ] **Page renames** — report as distinct category when page name changes.

### P2 — targeted enrichment

- [ ] **Use `figmaclaw diff` output to drive surgical `.md` updates.** Instead of re-enriching whole pages when frame hashes change, use the diff to identify exactly which frames were added/renamed and update only those rows in the body tables.

### P3 — polish and performance

- [ ] **Cache version history** per file in `.figma-sync/` to skip repeated lookups.
- [ ] **Structured version attribution** — group changes by designer ("Bartosz added 3 pages to Branding").
- [ ] **Figma label support** — surface user-supplied version labels in reports.

## Appendix: command reference

```bash
# Diff command (the core of the weekly report):
figmaclaw diff figma/ --since 7d [--format text|json] [--progress]

# Screenshot URLs for embedding in reports:
figmaclaw image-urls path/to/page.md --nodes NODE1,NODE2 [--scale 0.5]
```
