# Design Weekly Report — Design Doc

**Status:** In progress
**Owner:** Aviad (with Claude)
**Last updated:** 2026-04-05

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

## Approach

Replace git-history-based diffing with the **Figma Versions API**. For each tracked Figma file:

1. Fetch version history (`/v1/files/{key}/versions`, paginated).
2. Find the latest version **before** the cutoff (`--since 7d`).
3. Fetch the full file tree at that old version and at HEAD (`/v1/files/{key}?version=X`).
4. Compare frame lists page-by-page to detect structural changes.

This is implemented as a new `figmaclaw diff` CLI command.

## What we've done

### 1. `figmaclaw diff` — new CLI command (committed in `c1bcb1d`)

**File:** `figmaclaw/commands/diff.py`

**CLI:**
```bash
figmaclaw diff [TARGET] --since 7d [--format text|json] [--progress]
```

**Flow:**
1. Walk `TARGET/` and collect `{file_key: [page_ids]}` from `.md` frontmatter.
2. For each file, call `client.get_versions(file_key)` with pagination + early termination.
3. Filter to files with ≥1 version newer than the cutoff.
4. For each active file, fetch **current** file tree + **old** file tree in parallel (2 calls per file).
5. Compare `frames: list[str]` per page and report added/removed/renamed frames + flow edges.

**Output schema (JSON):**
```json
{
  "since": "2026-03-29",
  "until": "2026-04-05",
  "files": [
    {
      "file_key": "...",
      "file_name": "branding",
      "versions_in_range": [
        {"id": "...", "created_at": "...", "label": "...", "user": "Bartosz"}
      ],
      "pages": [
        {
          "page_node_id": "524:7042",
          "page_name": "round 4 - live and immersive",
          "figma_url": "https://www.figma.com/design/...",
          "is_new_page": true,
          "frames_before": 0,
          "frames_after": 44,
          "added_frames": [{"node_id": "...", "name": "..."}],
          "removed_frames": [...],
          "renamed_frames": [...],
          "added_flows": [...],
          "removed_flows": [...]
        }
      ]
    }
  ]
}
```

### 2. `figmaclaw image-urls` — new CLI command (committed in `c1bcb1d`)

**File:** `figmaclaw/commands/image_urls.py`

Gets Figma render URLs for specific frames without downloading. Used for embedding screenshots in reports (Slack, GitHub Discussions, markdown).

```bash
figmaclaw image-urls path/to/page.md --nodes 11:1,11:2 --scale 0.5
```

### 3. New FigmaClient methods (committed in `c1bcb1d`)

- `get_versions(file_key, *, max_pages, stop_when)` — paginated version history with early termination.
- `get_file_full(file_key, *, version)` — full file tree at a specific version.
- `get_versions_page()` / `_get_url()` — pagination helpers.

### 4. Performance fixes (committed in `c1bcb1d`)

**Problem:** First implementation was calling `get_page` per page, per file, per version. For a file with 30 pages that's 60 API calls at 14 req/min = 4 minutes per file, × 52 files = hours.

**Fix:** One `get_file_full` call returns the entire file tree (all pages, all frames). Now 2 calls per active file. And since only files with versions in the window are processed, inactive files need only 1 call (versions). Runtime dropped from hours to **3:26** for 52 files.

### 5. Pagination fix (committed in `c1bcb1d`)

**Problem:** The Figma versions endpoint returned only 30 versions by default. For files with high activity, the "old version" fell outside the first page — so we couldn't find a baseline to diff against.

**Fix:** `get_versions` now follows `pagination.next_page` automatically, up to 20 pages × 50 per page = 1000 versions. Early termination via `stop_when` callback avoids fetching more history than needed.

### 6. Connection error handling (local, not yet committed)

**Problem:** Large file trees (Mobile App: hundreds of pages and thousands of frames) can fail mid-download with `httpx.RemoteProtocolError: peer closed connection without sending complete message body`. The previous retry logic only handled HTTP 429/5xx, not connection errors. When a file download failed, `_diff_file` raised, `asyncio.gather(return_exceptions=True)` captured the exception, and the error was **silently dropped** in the result loop — so the user never saw that mobile-app had failed.

**Fix:**
- `_get` and `_get_url` in `figma_client.py` now retry on `RemoteProtocolError`, `ReadError`, `ReadTimeout`, `ConnectError` with exponential backoff.
- `_run` in `diff.py` now surfaces failures to stderr: `WARNING: failed to diff mobile-app (...): RemoteProtocolError: ...`.
- Also prints an info line when an active file has no structural changes: `mobile-app: 18 versions but no structural frame changes (edits only)`.
- Added 2 new tests: `test_retries_on_connection_drop`, `test_retries_on_read_timeout`.

## Interim results

Running `figmaclaw diff figma/ --since 7d` on the linear-git repo (2026-04-05):

```
Discovered 52 tracked Figma files
Fetching version history for 52 files...
  4 files had activity in the window
Fetching file trees for 4 active files...
```

**4 files with version activity in the last 7 days:**
1. **branding** — 45 versions by Bartosz *(structural changes confirmed)*
2. **mobile-app** — 18 versions by Bartosz *(still verifying)*
3. **web-app** — 11 versions by Bartosz *(still verifying)*
4. **untitled-ui-pro-styles-v70-...** — 2 versions *(still verifying)*

**Branding file — confirmed structural changes:**

| Page | Before | After | Change |
|---|---|---|---|
| round 2 | 70 | 91 | +21 frames |
| round 3 | 60 | 122 | +64 frames, -2 removed |
| round 4 - live and immersive | — | 44 | **NEW page** |
| round 5 - everything is a conversation | — | 15 | **NEW page** |

This matches exactly what the earlier background agent inferred from git history analysis — so the Figma API approach is producing correct results for at least one file.

## Open questions / needs verification

### CRITICAL: Why didn't mobile-app show structural changes?

The previous background agent found (from git log analysis):
- `mobile-app/pages/stage-widgets-improvements-6546-12154.md` — a **new page** with 10 frames
- `mobile-app/pages/community-page-294-35746.md` — one new frame `6702:37145` added
- `mobile-app/components/components-351-15063.md` — 1 component update

But `figmaclaw diff` reported mobile-app in "active files" (18 versions) yet produced **zero page-level changes**. Three possibilities:

1. **Connection drop silently swallowed (pre-fix bug)** — the retry fix should reveal this. **Currently testing.**
2. **Legitimate "edits only" case** — Bartosz changed frame contents (positioning, colors, layer tweaks) but didn't add/remove frames. Would show as "no structural changes" with the new progress logging.
3. **Frame extraction bug** — `from_page_node` doesn't pick up deeply nested frames, or section handling is wrong for one of the pages. Need to manually verify by diffing mobile-app at two versions.

**Action:** wait for the current diff run with retry fixes to complete. The stderr progress log will now tell us which case it is.

### Other open questions

- **Component pages:** Figma "component library" sections (stored in `frames: []` with `is_component_library=True`) — are those correctly compared? The Branding file only has regular pages, so component diffs are untested.
- **Autosave versions:** Figma autosaves appear as user `"Figma"` (not a real human). Should the report filter these out when attributing work to designers? Currently they're included in the version count.
- **Page renames:** If a page is renamed in Figma, its `page_node_id` stays the same but its name changes. The diff treats this as a modification, but we don't currently report page renames as a distinct category.
- **Page deletions:** If a designer deletes a whole page in Figma, `is_removed_page` isn't reported. Need to add a third category beyond `is_new_page` / modified.

## TODOs

### P0 — finish the diff tool

- [ ] **Verify mobile-app case** — wait for current run with connection retry to complete. Document why the 3 "active but no structural changes" files showed no changes. (Running now.)
- [ ] **Handle page deletions** — add `is_removed_page` field to PageDiff and report pages that existed in old version but not new.
- [ ] **Commit the retry + error-reporting fixes** — these are local edits not yet pushed. Run full test suite first.
- [ ] **Filter autosave versions in progress output** — when showing "N versions by ['Bartosz', 'Figma']", collapse 'Figma' into "autosaves" or drop them entirely.

### P1 — build the design weekly report generator

- [ ] **Create `.claude/skills/design-weekly-report.md`** — a skill that:
  1. Runs `figmaclaw diff figma/ --since 7d --format json` to get Figma activity.
  2. Reads DSG team activity from `linear/teams/DSG/issues/` via git log.
  3. Cross-references DSG issues to Figma pages by file_key / page_node_id matching.
  4. For each page with changes, calls `figmaclaw image-urls` to get screenshot URLs.
  5. Renders a markdown report with screenshots, tables, timeline, and cross-references.
  6. Posts to a GitHub Discussion (like the existing one at linear-git/discussions/4) or Slack.

- [ ] **Create `.github/workflows/weekly-report-design.yaml`** — CI automation modeled on the existing `weekly-report.yaml`.
- [ ] **Create `.github/design-report-prompt.md`** — the CI entrypoint prompt for Claude.

### P2 — targeted enrichment (future work, noted in memory)

- [ ] **Use `figmaclaw diff` output to drive surgical `.md` updates.** Instead of re-enriching whole pages when frame hashes change, use the diff to identify exactly which frames were added/renamed and update only those rows in the body tables. This would significantly reduce enrichment cost and disruption.

### P3 — polish and performance

- [ ] **Cache version history** — version lists don't change until new commits, so cache the `/versions` response per file in `.figma-sync/` to skip repeated lookups.
- [ ] **Use `get_file_meta(depth=1)` as a pre-filter** — before calling `get_versions`, check `lastModified` against cutoff. If it's older than the cutoff, skip the file entirely without calling `/versions`.
- [ ] **Structured version attribution** — the report should group changes by designer and show "Bartosz added 3 pages to Branding, 21 new frames across rounds 2–3" rather than per-file stats.
- [ ] **Figma label support** — Figma versions can have user-supplied labels ("WIP for Friday review"). When present, surface them prominently in the report.

## Architectural notes

### Why Figma API is the right source of truth

The `.md` files in `figma/` are a **projection** of Figma state. They're useful for agents (LLMs can read them without API access), but they lag reality by 1 hour (cron sync interval) and conflate multiple kinds of changes in their git history. The Figma API has:

- Full version history per file (paginated)
- Per-version file tree access
- User attribution on versions
- Timestamps for every version

This is the canonical source. The `.md` files are the derived form.

### Why not use git history for the diff

Tried and failed. Specific reasons:

1. **.md files were only first committed on 2026-03-31.** There is no "7 days ago" state to compare against in git.
2. **Bulk sync commits look identical to real changes.** A commit that created 50 files as part of initial tracking cannot be distinguished from 50 genuinely new Figma pages without parsing commit messages (fragile).
3. **Schema migrations** (frames dict→list) show up as diffs in git but aren't real content changes. Needs to be filtered out post-hoc.
4. **Enrichment bot commits** add descriptions to existing frames. These dwarf real designer work 100:1 in the commit log.

The Figma API bypasses all of this — it's a direct comparison of two points in time, regardless of when we started tracking.

### Why `get_file_full` instead of `get_page` per page

`get_page(file_key, page_node_id)` returns one page's tree per API call. A file with 30 pages = 30 calls × 2 versions = 60 calls per file. At the 14 req/min rate limit, that's ~4 minutes per file.

`get_file_full(file_key, version=X)` returns **everything** in one call. 2 calls per file, full stop. For 52 files × 2 = 104 calls total = ~7 minutes worst case. In practice we skip files with no activity (50/52) and only fetch fulls for the 2-4 active files, so the total is actually dominated by the version-history phase.

**Tradeoff:** `get_file_full` responses are large (sometimes 50+ MB for the Mobile App file). This introduces the connection-drop risk that we fixed with retries.

## Appendix: command reference

```bash
# The two new CLI commands:
figmaclaw diff figma/ --since 7d [--format text|json] [--progress]
figmaclaw image-urls path/to/page.md --nodes NODE1,NODE2 [--scale 0.5]

# Existing commands used in the pipeline:
figmaclaw sync      # nightly Figma → .md sync (hourly cron)
figmaclaw inspect   # check what needs enrichment
figmaclaw claude-run enrich ...  # LLM writes frame descriptions
```
