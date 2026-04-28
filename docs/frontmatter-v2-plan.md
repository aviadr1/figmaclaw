# Frontmatter v2: frames as index, not store

> **Canon source:** the design decisions D1..D10 originally proposed in this RFC are canonized in [`figmaclaw-canon` §5](../skills/figmaclaw-canon/SKILL.md#5-design-decisions). The data-contract argument (manifest=cache, frontmatter=state) is canonized in [§1](../skills/figmaclaw-canon/SKILL.md#1-three-layer-data-contract). This document is the **historical RFC** — kept for the rationale and tradeoff discussion behind each decision.

> **Status:** Implemented. All v2 work and section-level enrichment are complete (see "Changes needed" at the bottom). This plan superseded the "figmaclaw — LLM enrichment in CI/CD" Linear document (2026-04-01).

## The problem

`frames:` in frontmatter currently stores `{node_id: description}`. Descriptions
are LLM prose — they belong in the body, not frontmatter. This causes:
- Double work: LLM writes descriptions to frontmatter (set-frames) AND body (write-body)
- Sync drift: frontmatter and body descriptions can diverge
- Wrong abstraction: frontmatter is an INDEX (what exists, what changed), not a STORE

## What are the expensive operations?

| Operation | Cost | When |
|---|---|---|
| `get_file_meta()` | 1 Tier 1 API call | Every sync, per file |
| `get_page()` | 1 Tier 1 API call, full node tree | Per page when file version changed |
| `get_image_urls()` + download | 1 API call + bandwidth per batch | Per frame during enrichment |
| LLM screenshot reading | $$$ input tokens per image | Per frame during enrichment |
| LLM description generation | $$$ output tokens | Per frame during enrichment |

**Goal: avoid the bottom three (screenshots + LLM) unless a frame actually changed.**

## Decision tree: what triggers what

```
File version changed?  (manifest.version vs API)
  NO  → skip file entirely. ZERO API calls beyond the meta check.
  YES → fetch each page's full node tree (unavoidable — 1 API call per page)
          │
          Page hash changed?  (manifest.page_hash vs computed)
            NO  → skip page. No frontmatter write, no enrichment.
            YES → update frontmatter + manifest hashes
                    │
                    Page needs enrichment?  (frontmatter.enriched_hash vs manifest.page_hash)
                      NO  → done (structure changed but already re-enriched)
                      YES → WHICH frames need work?
                              │
                              Per-frame hash diff (computed vs frontmatter.enriched_frame_hashes):
                                NEW frame (in current, not in enriched)      → screenshot + describe
                                MODIFIED frame (hash differs)                → screenshot + re-describe
                                REMOVED frame (in enriched, not in current)  → remove from body
                                UNCHANGED frame (hash matches)               → SKIP (save $$$)
```

## What Figma gives us (and doesn't)

| Level | Figma provides | Change detection |
|---|---|---|
| File | `version`, `lastModified` | YES — any save |
| Page | Full node tree on request | We compute our own hash |
| Section | Nested in page tree | We compute our own hash |
| Frame | Nested in page tree | We compute our own hash |
| Per-node modified timestamp | **NOTHING** | Not available in REST API |
| Pixel-level changes | **NOTHING** | Only via screenshot comparison (expensive) |

Since Figma gives no per-node change tracking, we must compute and store our own
hashes from the full node tree we already fetch.

## Per-frame content hash

Computed from the frame's node subtree (depth-1 children):

```python
def compute_frame_hash(frame_node: dict) -> str:
    parts = [frame_node.get("name", "")]
    for child in frame_node.get("children", []):
        child_type = child.get("type", "")
        parts.append(f"{child.get('name', '')}:{child_type}")
        if child_type == "TEXT":
            parts.append(child.get("characters", ""))  # detect label changes
        if child_type == "INSTANCE":
            parts.append(child.get("componentId", ""))  # detect component swaps
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:8]
```

**Detects:** child elements added/removed/renamed, text content changes, component swaps.
**Ignores:** position/size changes, fill/stroke/effect changes, opacity. These are visual
polish, not content changes — descriptions rarely become stale from them.
**Cost:** zero extra API calls. Computed from data we already fetched.

## Where data lives

### Frontmatter (in .md file) — identity + structure + enrichment state

Frontmatter is the **machine-readable metadata** about the page. It answers: "what
exists, when was it last enriched, and is it stale?" — without calling any API.

```yaml
---
file_key: abc123
page_node_id: '7741:45837'
frames: ['11:1', '11:2', '46:42']
flows: [['11:1', '11:2']]
enriched_hash: 'b39103d8ad45cd38'
enriched_at: '2026-04-01T12:00:00Z'
enriched_frame_hashes: {'11:1': 'a3f2b7c1', '11:2': 'e4d9f8a2', '46:42': '1b3c5d7e'}
---
```

| Field | Updated by | Purpose |
|---|---|---|
| `file_key` | `sync` | Figma file identity (for API calls) |
| `page_node_id` | `sync` | Figma page identity (for API calls) |
| `frames` | `sync` | List of frame node IDs — what screens exist |
| `flows` | `sync`, `set-flows` | Prototype navigation edges |
| `enriched_hash` | `mark-enriched` | Page hash at time of last enrichment |
| `enriched_at` | `mark-enriched` | Timestamp of last enrichment (human-readable) |
| `enriched_frame_hashes` | `mark-enriched` | Per-frame content hashes at time of last enrichment |

**Why frontmatter, not manifest:**
- Self-contained — everything about a page lives in its file
- No merge conflicts — concurrent jobs on different pages never conflict
- No single point of failure — manifest corruption doesn't lose enrichment state
- Travels with the file — rename, move, copy to another repo
- Simpler tooling — `inspect` reads one file, no manifest dependency

### Manifest (`.figma-sync/manifest.json`) — sync engine cache only

Per-page entry:

```json
{
  "page_hash": "b39103d8ad45cd38",
  "frame_hashes": {"11:1": "a3f2b7c1", "11:2": "e4d9f8a2", "46:42": "1b3c5d7e"},
  "last_refreshed_at": "2026-04-01T12:00:00Z"
}
```

- `page_hash` — current structural hash. Used by sync to skip unchanged pages.
- `frame_hashes` — current per-frame content hashes. Computed during sync.
- `last_refreshed_at` — when sync last processed this page.

These are **cache** — recomputable from the API at any time. Losing them just means
sync re-fetches everything on the next run. No data loss.

### Body (in .md file) — all prose, LLM-owned

Page summary, section intros, description tables, Mermaid charts.
Never touched by sync. Only written by LLM via `write-body`.

## Enrichment detection

### Page level (fast, reads frontmatter + manifest only):

```
page_needs_enrichment = (
    enriched_hash is None                       # never enriched
    OR enriched_hash != manifest.page_hash      # structure changed
    OR body contains "(no description yet)"     # unfilled placeholders
    OR body contains "<!-- LLM:"                # scaffold comments
)
```

### Frame level (for pages that need enrichment):

Compare manifest `frame_hashes` (current) vs frontmatter `enriched_frame_hashes` (at last enrichment):

```
for frame_id in manifest.frame_hashes:
    if frame_id not in enriched_frame_hashes:
        → NEW frame. Screenshot + describe.
    elif manifest.frame_hashes[frame_id] != enriched_frame_hashes[frame_id]:
        → MODIFIED frame. Re-screenshot + re-describe.
    else:
        → UNCHANGED. Skip. Save $$$.

for frame_id in enriched_frame_hashes:
    if frame_id not in manifest.frame_hashes:
        → REMOVED frame. LLM removes row from body.
```

### What this saves on a 500-frame page:

Designer adds 2 new frames to a 500-frame page:
- **Without per-frame tracking:** 500 screenshots + LLM describes all 500 → ~$15
- **With per-frame tracking:** 2 screenshots + LLM describes 2, updates body → ~$0.10

## Commands

| Command | What it does | Reads | Writes | Touches body? |
|---|---|---|---|---|
| `sync` | Fetch structure from Figma | Figma API | Frontmatter (`frames`, `flows`) + manifest (`page_hash`, `frame_hashes`) | NEVER |
| `pull` | Bulk sync all tracked files | Figma API | Same as sync | NEVER |
| `write-body` | LLM writes page prose | stdin/flag | Body only, preserves frontmatter | YES |
| `mark-enriched` | Snapshot current hashes as enriched | Manifest hashes | Frontmatter `enriched_*` fields | NO |
| `mark-stale` | Force re-enrichment | — | Clears frontmatter `enriched_*` fields | NO |
| `inspect` | Check page structure + enrichment state | Frontmatter + body + manifest | Nothing | NO (read-only) |
| `screenshots` | Download frame PNGs | Manifest hash diff | PNG files in `.figma-cache/` | NO |
| `set-flows` | LLM writes inferred flows | `--flows` JSON | Frontmatter `flows` only | NO |

### Command renames from v1

| v1 name | v2 name | Why |
|---|---|---|
| `set-frames` | `set-flows` | No longer sets frame descriptions. Only sets flows. |
| `replace-body` | `write-body` | "Write" is the natural verb — the LLM is authoring prose, not destructively replacing. |
| `page-tree` | `inspect` | Covers both "show structure" and "check staleness." The tree is an implementation detail; the user wants to inspect the page's state. |
| `enrich` | `sync` | Already renamed in v1. |

### Removed

| v1 command | Why removed |
|---|---|
| `set-frames --frames` | Descriptions don't go in frontmatter. LLM writes them in body via `write-body`. |
| `set-frames --summary` | Summary is body prose. LLM writes it via `write-body`. |

## Exit code convention

All commands: exit 0 on success, exit 2 on error (not a figmaclaw file, etc.).
Business logic status (needs enrichment, missing descriptions) goes in JSON output,
**never** in exit codes.

```bash
figmaclaw inspect <file> --needs-enrichment --json
# {"needs_enrichment": true, "new_frames": ["11:3"], "modified_frames": ["11:1"], ...}
# Exit 0 always (unless actual error → exit 2)
```

## The enrichment flow

```
1. figmaclaw inspect <file> --needs-enrichment --json
     → reads frontmatter enriched_* + manifest current hashes
     → {"needs_enrichment": true, "new_frames": [...], "modified_frames": [...], "removed_frames": [...]}

2. figmaclaw screenshots <file> --stale
     → reads manifest hash diff
     → downloads only new + modified frames (not all 500)

3. LLM reads screenshots, writes updated body
     → only describes new + modified frames
     → preserves existing descriptions for unchanged frames
     → removes rows for deleted frames

4. figmaclaw write-body <file>
     → writes body, preserves frontmatter

5. figmaclaw mark-enriched <file>
     → copies manifest.frame_hashes → frontmatter.enriched_frame_hashes
     → copies manifest.page_hash → frontmatter.enriched_hash
     → sets frontmatter.enriched_at = now

6. figmaclaw inspect <file> --needs-enrichment --json
     → {"needs_enrichment": false}
```

## What we DON'T detect

- **Pixel-only changes** (designer redraws a screen with same structure): not detected
  by frame hash. Would require screenshot comparison (download + hash). Too expensive
  for the sync loop. Handle manually: `figmaclaw mark-stale <file>` to force re-enrichment.

- **Style changes** (colors, shadows, opacity): not detected. Descriptions focus on
  content and structure, not visual style. Rarely makes descriptions stale.

- **Changes in shared styles/components**: if a shared component changes, all frames
  using it are affected but their local node tree doesn't change. Not detected.
  Handle manually when design system updates happen.

## Key design decisions

### D1: Descriptions out of frontmatter

**Decision:** `frames:` stores only node IDs (list), not descriptions (dict).
Descriptions live exclusively in the body.

**Why:** Descriptions are LLM prose. Storing them in frontmatter created duplication
(frontmatter AND body), sync drift (they diverge), and double work (set-frames + write-body).
Frontmatter is a machine index — it tracks what exists and what changed, not what things
look like.

**Tradeoff:** `parse_frame_descriptions()` goes away. Any tool that needs descriptions
must read the body tables or call the LLM. This is fine — machines need IDs and hashes,
humans and LLMs need prose.

### D2: Per-frame content hashes for surgical enrichment

**Decision:** Compute a content hash per frame (depth-1 children: names, types, text
content, component IDs). Store enriched hashes in frontmatter, current hashes in manifest.
Diff them to find exactly which frames changed.

**Why:** A 500-frame page where 2 frames changed should re-enrich 2 frames, not 500.
Without per-frame tracking, any structural change triggers full-page re-enrichment
(500 screenshots + LLM on all 500 = ~$15). With it: 2 screenshots + LLM on 2 = ~$0.10.

**Tradeoff:** Frontmatter gets `enriched_frame_hashes` (one flow-style dict line, ~4KB
for 500 frames). Worth it — the current `frames:` with descriptions is already bigger.

**Why depth-1:** Hashing the full recursive subtree would catch every nested change but
also trigger on trivial layout tweaks. Depth-1 catches meaningful changes (elements
added/removed, text changed, component swapped) while ignoring noise (position shifts,
style tweaks). Descriptions rarely become stale from a color change.

### D3: Enrichment state in frontmatter, not manifest

**Decision:** `enriched_hash`, `enriched_at`, `enriched_frame_hashes` live in the .md
file's frontmatter. Manifest only holds sync cache (`page_hash`, `frame_hashes`).

**Why:**
- **Self-contained**: `inspect` reads one file to check enrichment status. No manifest dependency.
- **No merge conflicts**: concurrent jobs on different pages never conflict. Manifest is a
  single file — two writers = merge conflict (we already hit this).
- **No single point of failure**: manifest corruption loses cache (recomputable). Enrichment
  state survives because each page carries its own.
- **Portable**: rename, move, or copy a page to another repo — enrichment state travels with it.
- **Git-friendly**: each page's enrichment history is in its own git blame.

**Tradeoff:** Frontmatter is bigger. Acceptable — it's one extra line of flow-style YAML.

### D4: Manifest is cache, frontmatter is state

**Decision:** Manifest stores only sync engine cache. Frontmatter stores persistent state.

**Manifest (cache, recomputable, lossy):**
- `page_hash` — "should I re-fetch this page?" Skip decision.
- `frame_hashes` — "what do the frames look like now?" Current snapshot.
- `last_refreshed_at` — "when did I last check?" Timing.

**Frontmatter (state, persistent, authoritative):**
- `frames` — "what screens exist?" Structure.
- `flows` — "how do they connect?" Navigation.
- `enriched_*` — "is the body up to date?" Staleness.

If the manifest is deleted, sync re-fetches everything on the next run. Zero data loss.
If frontmatter is deleted, we lose the page's identity and enrichment history.

### D5: `mark-enriched` as separate command

**Decision:** `write-body` writes body only. `mark-enriched` snapshots hashes. Two
separate commands, two separate concerns.

**Why:** `write-body` might be used to fix a typo (not a full enrichment). Coupling
it with hash snapshotting would mark a page as fully enriched when it isn't. The enrichment
pipeline calls both in sequence; manual edits call only `write-body`.

**Tradeoff:** One extra command in the enrichment flow. Trivial cost for correct semantics.

### D6: Exit codes for errors only

**Decision:** All commands exit 0 on success. Exit 2 for actual errors (not a figmaclaw
file, missing manifest, etc.). Business logic status (`needs_enrichment`, `missing_descriptions`)
is in the JSON output, never in exit codes.

**Why:** Exit 1 conventionally means error. Using it for "needs enrichment" breaks
standard shell conventions — `set -e` scripts abort, CI marks the step as failed,
error monitoring triggers. JSON output is the right channel for non-error status.

### D7: Frame hash excludes position/size/style

**Decision:** `compute_frame_hash` hashes child names, types, text content, and component
references. It ignores absolute position, size, fills, strokes, effects, opacity.

**Why:** Descriptions say "login screen with email input and Sign In button." Moving the
button 10px doesn't make that stale. Changing the button text from "Sign In" to "Log In"
does. The hash should match what makes descriptions stale, not what makes pixels different.

**What we miss:** A designer completely redraws a frame with the same child structure.
This is rare and handled manually (`mark-stale`).

### D8: Command naming — verbs match semantics

| Command | Verb | Why this verb |
|---|---|---|
| `sync` | sync | Synchronizes structure from Figma to local |
| `pull` | pull | Pulls all tracked files (git analogy) |
| `write-body` | write | LLM is authoring prose — natural verb for content creation |
| `mark-enriched` | mark | Sets a flag/state — "mark as done" |
| `mark-stale` | mark | Sets a flag/state — "mark as needing redo" |
| `inspect` (currently `page-tree`) | inspect | Examines state without changing anything — read-only |
| `set-flows` | set | Writes a specific field value — "set flows to X" |
| `screenshots` | (noun) | Downloads artifacts — the noun is the thing you get |

### D9: Current hashes in manifest, not frontmatter

**Decision:** Current frame hashes (`frame_hashes`) live in the manifest only.
Enriched frame hashes (`enriched_frame_hashes`) live in frontmatter only.

**Why:** Per D4 ("manifest is cache, frontmatter is state"): current frame hashes
are a cache of Figma API state — recomputable, lossy. They belong in the manifest.
Enriched frame hashes are persistent state — they record what things looked like when
we last wrote descriptions. They belong in frontmatter.

**Alternative considered:** Adding `frame_hashes` to frontmatter for self-contained
staleness detection without manifest dependency (D3). Rejected because:
- Duplicates data in two places (manifest AND frontmatter)
- Bloats large pages (~10KB of hashes for 500-frame pages)
- Increases git churn (every frame content change rewrites frontmatter, not just the manifest)
- The manifest is committed to the repo and always available in practice
- `screenshots --stale` already reads the manifest — `inspect` follows the same pattern

**Fallback:** If manifest is missing, treat all frames as stale (safe, triggers full re-enrichment).

### D10: Section-level enrichment via per-frame hash aggregation

**Decision:** No per-section hashes or timestamps in frontmatter. Section staleness
is computed at runtime by mapping stale frames (per-frame hash diff between manifest
and frontmatter) to sections (body parsing via `parse_sections()`).

**Why:** Per-frame hashes already exist. Computing "which sections are stale" is a
join of two existing data sources: `manifest.frame_hashes` (current) ×
`enriched_frame_hashes` (at enrichment) × `parse_sections()` (section→frames map).
Adding per-section hashes would be redundant aggregation of per-frame data.

**`mark-enriched` remains page-level:** Called only after ALL stale sections are
re-enriched. Cannot call after each section — that would mark other still-stale
sections as current.

**Section-level enrichment commands:**
- `write-body --section <node_id>` — surgically replace one section
- `screenshots --section <node_id>` — download only one section's frames
- `inspect --json` — reports `pending_frames` and `stale_frames` per section
- `claude-run --section-mode` — for large pages, enriches one section at a time

## CI enrichment pipeline (implemented)

These invariants were established during the CI enrichment work and must be preserved.

### Design law

- figmaclaw is a **pure data-fetch tool** — zero LLM dependency. No `anthropic` SDK,
  no `figma_llm.py`, no LLM model parameters.
- LLM enrichment runs via **Claude Code CLI** (`claude -p`) using `CLAUDE_CODE_OAUTH_TOKEN`
  — NOT `ANTHROPIC_API_KEY`, NOT the `anthropic` Python SDK.
- The enrichment skill (`figma-enrich-page`) lives in the figmaclaw package and is
  auto-installed at CI runtime. Consumer repos (linear-git) never contain skill files.

### Architecture

```
Trigger (Figma webhook or hourly cron)
  ↓
figmaclaw pull / apply-webhook          (reusable workflow in figmaclaw repo)
  → fetches Figma API
  → updates frontmatter (frames, flows)
  → writes skeleton body for NEW pages only
  → auto-commit per changed page
  → uses: FIGMA_API_KEY only
  ↓
claude-run                              (reusable workflow in figmaclaw repo)
  → claude_run.py finds changed files (git diff or needs_enrichment filter)
  → for each file, runs figma-enrich-page skill:
      1. page-tree --json             → check needs_enrichment
      2. screenshots --pending        → download PNGs for undescribed frames
      3. LLM reads screenshots, writes descriptions
      4. write-body                   → write prose body (preserves frontmatter)
      5. set-flows                    → write inferred flows to frontmatter
      6. mark-enriched               → snapshot hashes to frontmatter
      7. git commit + push per file
  → uses: CLAUDE_CODE_OAUTH_TOKEN, FIGMA_API_KEY
```

### CI workflow structure (in consumer repos like linear-git)

Consumer repos contain **thin caller workflows** that reference reusable workflows
from `aviadr1/figmaclaw`. All logic lives upstream.

- **figmaclaw-sync.yaml** — hourly cron. Calls `sync.yml` then `claude-run.yml`
  with `needs_enrichment: true`. Concurrency group: `figma-git-sync`,
  `cancel-in-progress: false` (never drop a scheduled sync).

- **figmaclaw-webhook.yaml** — `repository_dispatch`. Calls `webhook.yml` then
  `claude-run.yml` with `changed_only: true`. Same concurrency group,
  `cancel-in-progress: true` (debounce rapid designer saves).

### CLI commands for CI enrichment (implemented 2026-04-03)

All enrichment tooling is now proper Click commands in figmaclaw — no standalone
scripts, no `importlib.resources` path resolution, no import hacks.

- **`figmaclaw claude-run`** — discovers files, filters by enrichment status
  (`--needs-enrichment`, `--changed-only`, `--max-files`), invokes `claude -p`
  per file with a prompt template. Defaults to bundled `figma-batch-enrich.md`
  prompt. Outputs stream-json to stdout.

- **`figmaclaw stream-format`** — reads stream-json from stdin, writes
  human-readable CI log lines to stdout. Appends summary to `$GITHUB_STEP_SUMMARY`
  in GitHub Actions.

CI workflow usage:
```bash
figmaclaw claude-run figma/ \
  --needs-enrichment \
  --model claude-sonnet-4-6 \
  --max-turns 80 \
  | tee /tmp/claude-raw.jsonl \
  | figmaclaw stream-format
```

Installed via `uv tool install` (isolated CLI, no import needed).
Tested: 23 tests in `tests/test_claude_run.py`.

**History:** These were previously standalone scripts (`scripts/claude_run.py`,
`.github/stream-formatter.py`) in the linear-git consumer repo. Ported upstream
to figmaclaw on 2026-04-03 after a syntax error in the standalone script broke
CI enrichment for 24+ hours. Making them proper CLI commands eliminates the
class of bugs where import paths, `importlib.resources`, or `uv tool install`
isolation prevent Python from finding the scripts.

### What must NOT change in figmaclaw

- `pull_logic.py` — correct as-is (writes frontmatter + skeleton body; no LLM)
- `screenshots.py`, `set_flows.py`, `inspect.py` — correct CI tools
- No `figma_llm.py` — figmaclaw has zero LLM dependency
- No `anthropic` in `pyproject.toml`
- `enrich.py` — fine for structural re-sync (no LLM)

### Known issue: skeleton body churn

`figmaclaw pull` writes a skeleton body for pages whose Figma structure changed
(even if they had existing prose). The enrichment job runs immediately after and
fixes this. Net result: skeleton commit, then ~seconds later enriched commit.
Acceptable — the skeleton is a brief intermediate state, not permanent loss.

Long-term fix: `write_page()` could skip writing the skeleton body if an existing
body is present (only write frontmatter). Separate figmaclaw change, not needed
for correctness.

### One-time migration

To enrich all existing bare pages, trigger `claude-run.yml` via `workflow_dispatch`:
```
target: figma/
needs_enrichment: true
changed_only: false
model: claude-sonnet-4-6
max_turns: 80
max_files: 0
```

## Changes needed

### figmaclaw repo — done:

1. ~~**`figma_frontmatter.py`** — `frames: list[str]`, `enriched_*` fields, backward-compat validator~~ ✅
2. ~~**`figma_sync_state.py`** — `frame_hashes` in `PageEntry`~~ ✅
3. ~~**`figma_hash.py`** — `compute_frame_hash()` + `compute_frame_hashes()`~~ ✅
4. ~~**`figma_render.py`** — `_build_frontmatter()` takes `list[str]`~~ ✅
5. ~~**`figma_parse.py`** — `parse_frame_descriptions()` removed, frontmatter handles both formats~~ ✅
6. ~~**`pull_logic.py`** — computes/stores `frame_hashes` in manifest~~ ✅
7. ~~**`commands/set_flows.py`** — replaces `set_frames.py`~~ ✅
8. ~~**`commands/write_body.py`** — replaces `replace_body.py`~~ ✅
9. ~~**`commands/mark_enriched.py`**~~ ✅
10. ~~**`commands/mark_stale.py`**~~ ✅
11. ~~**`main.py`** — v2 commands registered~~ ✅

### figmaclaw repo — done (2026-04-03, v2 migration):

12. ~~**`commands/page_tree.py`** → **`commands/inspect.py`** — Renamed command, added `--needs-enrichment` flag~~ ✅
13. ~~**`commands/screenshots.py`** — Added `--stale` flag with hash-based frame filtering~~ ✅
14. ~~Tests renamed and updated (`test_inspect.py`, `test_write_body.py`, stale comments fixed)~~ ✅
15. ~~Docs updated (format spec, invariants, body-preservation, TODO.md)~~ ✅

### figmaclaw repo — done (2026-04-03):

16. ~~**`commands/claude_run.py`** — `figmaclaw claude-run` Click command. Ported from linear-git's `scripts/claude_run.py`, fixed syntax error, proper CLI integration~~ ✅
17. ~~**`commands/stream_format.py`** — `figmaclaw stream-format` Click command. Ported from linear-git's `.github/stream-formatter.py`~~ ✅
18. ~~**`prompts/figma-batch-enrich.md`** — bundled enrichment prompt template. Ported from linear-git's `prompts/`~~ ✅
19. ~~**`.github/workflows/claude-run.yml`** — reusable CI workflow. Uses `figmaclaw claude-run` and `figmaclaw stream-format` CLI commands (no importlib hacks)~~ ✅
20. ~~**`CLAUDE.md`** — ecosystem ownership documented (figmaclaw/issueclaw = general-purpose tooling, consumer repos = data only)~~ ✅
21. ~~**`tests/test_claude_run.py`** — 23 tests: syntax validation, enrichment detection, file collection, CLI dry-run~~ ✅
22. ~~**Template workflows** — `figmaclaw-sync.yaml` template and bundled workflow updated to include enrichment step via upstream `claude-run.yml`~~ ✅

### linear-git repo — done (2026-04-03):

23. ~~**figmaclaw-sync.yaml** — enrichment step now calls `aviadr1/figmaclaw/.github/workflows/claude-run.yml@main` instead of local `claude-run.yaml`~~ ✅
24. ~~**figmaclaw-webhook.yaml** — same change, enrichment calls upstream~~ ✅
25. ~~**`scripts/claude_run.py`** — syntax error fixed (orphaned `except` block). File kept as fallback for local `claude-run.yaml` manual dispatch~~ ✅
26. ~~**AGENTS.md** — ecosystem ownership documented~~ ✅

### linear-git repo — done (2026-04-03, v2 migration):

27. ~~**All figma data files** already use v2 list format (zero dict-format files remain)~~ ✅
28. ~~**Remove local tooling copies** — deleted `scripts/claude_run.py`, `.github/stream-formatter.py`, `prompts/figma-batch-enrich.md`, `.github/workflows/claude-run.yaml`~~ ✅
29. ~~**AGENTS.md** — updated for v2 frontmatter format~~ ✅

### figmaclaw repo — done (2026-04-03, v2 migration):

30. ~~**`commands/page_tree.py`** → **`commands/inspect.py`** — Renamed, added `--needs-enrichment` flag~~ ✅
31. ~~**`commands/screenshots.py`** — Added `--stale` flag~~ ✅
32. ~~**CI prompt** (`prompts/figma-batch-enrich.md`) — Updated to use `inspect` and `screenshots --stale`~~ ✅
33. ~~All skills and workflow references updated~~ ✅

### figmaclaw repo — done (2026-04-03, section-level enrichment):

34. ~~**`figma_md_parse.py`** — `section_line_ranges()` helper for section boundary detection~~ ✅
35. ~~**`commands/inspect.py`** — per-section `pending_frames` and `stale_frames` in `--json` output~~ ✅
36. ~~**`commands/screenshots.py`** — `--section <node_id>` filter~~ ✅
37. ~~**`commands/write_body.py`** — `--section <node_id>` surgical section replacement~~ ✅
38. ~~**`commands/claude_run.py`** — `--section-mode` flag, `pending_sections()`, `needs_finalization()`, section-by-section orchestration~~ ✅
39. ~~**`prompts/figma-section-enrich.md`** — per-section enrichment prompt~~ ✅
40. ~~**`prompts/figma-section-finalize.md`** — finalization prompt (page summary + mermaid + mark-enriched)~~ ✅
41. ~~**`.github/workflows/claude-run.yml`** — `min_frames`, `max_frames`, `section_mode` inputs~~ ✅
42. ~~**Templates** — two-pass enrichment (bulk ≤80 frames + section-mode >80 frames)~~ ✅
43. ~~**Design decisions D9, D10** documented in this plan~~ ✅
44. ~~**Tests** — 264 passing (19 new tests for section-level features)~~ ✅

**All v2 migration and section-level enrichment items are complete.**
