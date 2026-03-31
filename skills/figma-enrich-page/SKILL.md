---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and inferred screen flows.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with descriptions for every frame, a page summary, and inferred screen flow edges.

## When to use

- A page under `figma/*/pages/` has `(no description yet)` placeholders
- You want to re-sync descriptions for a specific page without a full pull

## Preferred approach — use the CLI

Run `figmaclaw enrich` directly. It handles everything: fetches the page from Figma REST API, merges existing descriptions, writes the file back, and updates the manifest.

```bash
figmaclaw enrich figma/web-app/pages/<page-slug>.md
```

Options:
- `--auto-commit` — git commit the result

### What it reads from Figma
- `GET /v1/files/{file_key}?depth=1` — file name and version (for manifest update)
- `GET /v1/files/{file_key}/nodes?ids={page_node_id}` — full page node tree (sections + frames)

### What it writes
- The `.md` file in-place — updated frontmatter (`frames`, `flows`) and body (descriptions, summary, Mermaid chart)
- `.figma-sync/manifest.json` — stamps the new page hash so the next pull skips this page

## Fallback — manual steps (if figmaclaw is not installed)

### 1 — Read the existing .md to get file_key and page_node_id

The frontmatter always contains:
```yaml
file_key: <KEY>
page_node_id: <ID>
```

### 2 — Fetch page structure from Figma MCP

Use `get_metadata` with the page node ID (the CANVAS node).

```
get_metadata(fileKey=<file_key>, nodeId=<page_node_id>)
```

If the output is too large (saved to a temp file), parse it with:
```bash
python3 -c "
import json
with open('<tool-result-path>') as f:
    data = json.load(f)
text = data[0]['text']
lines = [l for l in text.split('\n') if '<section' in l or ('<frame' in l and l.count('  ') <= 4)]
for l in lines:
    print(l[:120])
"
```

### 3 — Generate descriptions

For each frame, write ≤20 words describing:
- **What it shows** — the screen state (e.g. "captions bar visible", "modal open")
- **What makes it distinct** — the key difference from sibling frames

Signals from the XML tree:
- Frame name (e.g. `captions / widget highlighted` vs `captions / widget default`) → state variant
- Child element names (e.g. `mic-selection`, `hover`, `Modal`, `Input field`) → what UI is overlaid
- Frame size: 1440×900 = full screen; <200px = component/icon detail

### 4 — Infer screen flows

Look for sequences implied by frame names and nesting:
- `default` → `highlighted` (user interaction)
- `enabled` → `options` (user opens settings)
- Full-screen frames with overlay children (modal, hover, popup) are destinations from the base screen

### 5 — Write the enriched .md

Overwrite the file with:
- Frontmatter: `frames` dict (node_id → description) and `flows` list
- Body: page summary paragraph, section tables with descriptions filled in, Mermaid flowchart if flows exist

### 6 — Commit and push

```bash
git add figma/web-app/pages/<page-slug>.md
git commit -m "sync: enrich <page-name> with descriptions and flows"
git push
```

## What information you need from a page

| What to fill | Source |
|---|---|
| Frame descriptions | Screenshots (or rendered image) per frame — use `get_screenshot` or `get_design_context` from Figma MCP |
| Page summary | Screenshots + understanding of the whole page |
| Section intros | Frame names + metadata structure (often no screenshots needed) |
| Flowchart edges | `reactions` field in the Figma API node tree (prototype connections) — fetched automatically by `figmaclaw enrich` |

**On frame names:** Even when names are descriptive (e.g., `captions / widget highlighted`), screenshots are still needed for useful descriptions. Names tell you the state variant but not what UI elements are present, how the layout differs from siblings, or whether the name accurately reflects what's rendered.

**On flowcharts:** Screenshots don't tell you flows. The Figma API node tree includes a `reactions` field on each frame — these are the prototype arrows (click/hover interactions) connecting frames. That's the authoritative source. `figmaclaw enrich` extracts these automatically. If a page has no prototype reactions, no Mermaid block is generated — that's correct behavior, not a gap.

## Notes

- The `reserach` section typo in some Figma files is real — preserve it as-is so the node_id mapping stays correct
- Small frames (≤200px) inside sections are usually icon/component details, not full screens
- If the page has no flows at all, omit the `flows` key and the Mermaid block entirely
