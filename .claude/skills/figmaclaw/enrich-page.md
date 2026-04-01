---
description: Enrich figmaclaw .md files with frame descriptions using page-tree and set-frames. Use when a page has (no description yet) placeholders.
---

# figmaclaw/enrich-page

Enrich one or more figmaclaw `.md` files with frame descriptions.

The agent is the LLM — use `page-tree` to read what needs describing, reason about
it, then use `set-frames` to write the descriptions back.

## Workflow

### 1. Inspect the page

```bash
figmaclaw page-tree <md-path> --missing-only
```

Outputs sections, frame names, node IDs, and which frames lack descriptions.
No Figma API call — reads the `.md` directly.

Use `--json` for structured output when processing multiple files.

Exit code: `0` if all frames have descriptions, `1` if any are missing.

### 2. Generate descriptions

For each frame that needs a description, reason about it from the frame name,
section name, and sibling frame names. Key signals:

- Frame name variants (e.g. `default` → `hover` → `active`) → state progression
- Section name gives the feature context
- Frame size in the name (e.g. `fullhd`, `mobile web`) → viewport variant
- Child element names in nested frames → what UI is present

For visual ambiguity, use the Figma MCP:
```
get_design_context(fileKey=<file_key>, nodeId=<node_id>)
```
file_key and page_node_id are in the .md frontmatter.

Write ≤20 words per description:
- **What it shows** — the screen state
- **What makes it distinct** — the key difference from siblings

### 3. Write descriptions back

```bash
figmaclaw set-frames <md-path> --frames '{"node_id": "description", ...}'
```

Or pipe JSON:
```bash
echo '{"node_id": "description"}' | figmaclaw set-frames <md-path>
```

Surgically updates the `frames:` frontmatter dict only. Body prose is never
touched by code — the LLM owns it. No Figma API call.

Use `--summary "..."` to set or replace the page summary paragraph.
Use `--auto-commit` to git commit the result.

### 4. Verify

```bash
figmaclaw page-tree <md-path>
```

Should exit 0 with all frames showing ✓.

## Re-syncing structure (separate from descriptions)

If the page structure changed in Figma and you want to pull in new frames before
enriching:

```bash
figmaclaw sync <md-path>
```

Re-fetches the page from Figma REST API, updates only the frontmatter.
The LLM-authored body is never overwritten. Does not call an LLM.

## Bulk enrichment pattern

```bash
# Find all pages with missing descriptions
for f in figma/**/*.md; do
  figmaclaw page-tree "$f" --missing-only --json 2>/dev/null && echo "$f"
done

# Or: find files where page-tree exits 1 (has missing descriptions)
find figma -name '*.md' | while read f; do
  figmaclaw page-tree "$f" --missing-only >/dev/null 2>&1 || echo "$f"
done
```

Then enrich each file with the workflow above.

## Notes

- `set-frames` only works on canonical figmaclaw-rendered `.md` files. If a file
  was hand-written, run `figmaclaw sync` first to canonicalize it.
- The `frames:` dict in frontmatter is the machine-readable source of truth.
  Body tables are LLM-authored prose — code never touches them.
- Small frames (≤200px in size, visible in the frame name or section name) are
  usually icon/component details — describe as component, not full screen.
- If a frame has no name (`|   |`), note the section context and describe from
  sibling frames.
