# Design Weekly Report — Design Doc

**Status:** v1 complete — diff tool + weekly report skill + screenshot pipeline all working
**Owner:** Aviad (with Claude)
**Last updated:** 2026-04-06

## Goal

Generate a **weekly design report** that captures what designers actually changed in the company's Figma files over the past week, combined with the design team's Linear activity. The report should be:

1. **Accurate** — show real designer work, not bot noise (enrichment, schema migrations, initial tracking).
2. **Visual** — include Figma screenshots inline, not just text.
3. **Structured** — tables, cross-references between Linear issues and Figma pages.
4. **Automatable** — generated on a schedule and posted to a GitHub Discussion.

This is the design equivalent of the existing `friday-report` skill that produces an engineering weekly report from Linear activity.

## What's done

### 1. `figmaclaw diff` — CLI command ✅

**Commits:** `c1bcb1d` (initial), `684a3ed` (perf + bug fixes)

```bash
figmaclaw diff [TARGET] --since 7d [--format text|json] [--progress]
```

**Architecture:**

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

**Runtime: ~2:18 for 52 tracked files.**

### 2. Performance optimizations ✅

| Optimization | Before | After |
|---|---|---|
| `get_file_shallow(depth=3)` instead of `get_file_full` | 50+ MB per file, 3.6 GB RAM, hangs | ~KB per file, completes fast |
| `get_file_meta` pre-filter before version lookup | 52 version lookups (~4 min) | ~16-21 lookups (~1 min) |
| Rate limit 14 → 30 RPM | ~4.3s between requests | ~2s between requests |
| Batch page IDs with 400 fallback | URL limit errors | Batches of 10 |
| Per-file 5-min timeout | Hangs forever | Fails cleanly |
| httpx timeout 120s → 300s | Timeouts on large responses | Completes |

### 3. Bug fixes ✅

- **VersionSummary nullable fields:** Figma returns `null` for `label`/`description` on autosave versions. Pydantic rejected these, causing `FigmaAPIValidationError` silently swallowed by `asyncio.gather`. All 52 files silently failed → fixed with `str | None`.
- **Silent error dropping:** Version lookup failures now reported in progress output.
- **Connection retry:** `_get`/`_get_url` retry on connection errors with exponential backoff.

### 4. `figmaclaw image-urls` — CLI command ✅

Gets Figma render URLs for specific frames. Used to download screenshots for embedding in reports.

```bash
figmaclaw image-urls path/to/page.md --nodes 11:1,11:2 --scale 0.5
# → {"file_key": "...", "images": {"11:1": "https://figma-alpha-api.s3..."}}
```

### 5. Weekly report skill ✅

**File:** `linear-git/.agents/skills/design-weekly-report.md`

The skill orchestrates the full pipeline:
1. `figmaclaw diff figma/ --since 7d --format json` → structural changes
2. Pick 2-3 representative frames per changed page
3. `figmaclaw image-urls` → temporary S3 render URLs → `curl` download
4. `gh release create --draft` → permanent GitHub-hosted screenshot URLs
5. Grep DSG Linear issues for cross-references
6. Build markdown report with embedded screenshots
7. Post to GitHub Discussion via GraphQL mutation

**First report:** [Discussion #10](https://github.com/gigaverse-app/linear-git/discussions/10) — Mar 30–Apr 6 2026, 26 screenshots, 3 files, 10 pages.

### 6. Tests ✅

16 tests passing:
- 3 new tests for nullable fields and null-label version parsing
- All integration tests updated for `get_file_shallow` + `get_file_meta` mocks

### 7. Verified results ✅

Running `figmaclaw diff figma/ --since 7d` on 2026-04-06:

| File | Versions | Pages changed | Summary |
|---|---|---|---|
| **branding** | 44 (Bartosz) | 4 pages (2 new) | +144, -2 frames |
| **mobile-app** | 18 (Bartosz) | 4 pages (1 new) | +63, -1 frames |
| **web-app** | 10 (Bartosz, Abhishek) | 2 pages (1 new) | +20 frames |
| **untitled-ui** | 2 | 0 | edits only |

## TODOs

### P0 — CI automation

- [ ] **Create `.github/workflows/weekly-report-design.yaml`** — run weekly (Fridays), modeled on existing `weekly-report.yaml`.
- [ ] **Create `.github/design-report-prompt.md`** — CI entrypoint prompt that invokes the skill.

### P1 — remaining diff improvements

- [ ] **Handle page deletions** — add `is_removed_page` to `PageDiff`.
- [ ] **Filter autosave versions** — collapse "Figma" autosaves from designer attribution.
- [ ] **Page renames** — report as distinct category.

### P2 — targeted enrichment

- [ ] **Use diff output to drive surgical `.md` updates.** Instead of re-enriching whole pages, use the diff to identify exactly which frames were added/renamed and update only those rows.

### P3 — polish and performance

- [ ] **Cache version history** per file in `.figma-sync/` to skip repeated lookups.
- [ ] **Structured version attribution** — "Bartosz added 3 pages to Branding" instead of raw counts.
- [ ] **Figma label support** — surface user-supplied version labels in reports.

## Appendix: command reference

```bash
# Diff — structural changes from Figma Versions API:
figmaclaw diff figma/ --since 7d [--format text|json] [--progress]

# Screenshot render URLs (temporary S3, used for download):
figmaclaw image-urls path/to/page.md --nodes NODE1,NODE2 [--scale 0.5]

# Download screenshots to local cache:
figmaclaw screenshots path/to/page.md [--pending|--stale]
```

## Appendix: screenshot hosting

Screenshots are uploaded as **GitHub release assets** (permanent URLs) via:

```bash
gh release create "design-report-YYYY-wNN" \
  --repo gigaverse-app/linear-git \
  --draft \
  /tmp/design-report-screenshots/*.png
```

Asset URLs: `https://github.com/gigaverse-app/linear-git/releases/download/{tag}/{file}.png`

These render inline in GitHub Discussion markdown as `![alt](url)`.
