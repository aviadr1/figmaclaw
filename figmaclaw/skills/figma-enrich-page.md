---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and inferred screen flows.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with LLM-generated descriptions for every frame, a page summary, and inferred screen flow edges.

## When to use

- A page under `figma/*/pages/` has `(no description yet)` placeholders
- The ANTHROPIC_API_KEY quota was exhausted during the automated pull
- You want to re-generate descriptions for a specific page without a full pull

## Preferred approach — use the CLI

Run `figmaclaw enrich` directly. It handles everything: fetches the page from Figma REST API, merges existing descriptions, calls Claude, writes the file back, and updates the manifest.

```bash
figmaclaw enrich figma/web-app/pages/<page-slug>.md
```

Options:
- `--force` — re-generate all descriptions, not just missing ones
- `--no-llm` — re-fetch and re-render only (no Claude call)
- `--auto-commit` — git commit the result

### What it reads from Figma
- `GET /v1/files/{file_key}?depth=1` — file name and version (for manifest update)
- `GET /v1/files/{file_key}/nodes?ids={page_node_id}` — full page node tree (sections + frames)

### What it writes
- The `.md` file in-place — updated frontmatter (`frames`, `flows`) and body (descriptions, summary, Mermaid chart)
- `.figma-sync/manifest.json` — stamps the new page hash so the next pull skips this page

## Fallback — manual steps (if figmaclaw is not installed)

### 1 — Read the existing .md to get file_key and the frame inventory

The frontmatter contains `file_key` and `page_node_id`. The table body already lists every frame with its node ID — figmaclaw wrote this skeleton on the last sync. **Do not call `get_metadata` or fetch page structure from Figma.** The inventory is already here.

Parse the table for all rows where Description is `(no description yet)` to get the pending node IDs.

### 2 — Enrich in batches of 8 using subagents

Process frames in batches of 8. For each batch, spawn a subagent:

> "Here are 8 Figma frames from file `<file_key>`. Call `get_screenshot` for all 8 node IDs **in parallel** (single tool-use response):
> `<node_id_1>`, `<node_id_2>`, ... `<node_id_8>`
>
> For each frame, write a description of 1–3 sentences covering:
> - What the screen shows and its current state
> - Key UI elements visible (inputs, modals, CTAs, overlays, toggles, etc.)
> - What makes it visually distinct from its siblings
>
> Return only a JSON object: `{ "<node_id>": "<description>", ... }`"

After the subagent returns, **immediately write those descriptions into the `.md`** (Edit the table rows), then proceed to the next batch. Do not accumulate batches before writing.

This keeps screenshots out of the main context — each subagent's images are discarded after it returns text.

### 3 — Infer screen flows

Look for sequences implied by frame names and nesting:
- `default` → `highlighted` (user interaction)
- `enabled` → `options` (user opens settings)
- Full-screen frames with overlay children (modal, hover, popup) are destinations from the base screen

### 4 — Write the enriched .md

Update the file with:
- Frontmatter: `frames` dict (node_id → description) and `flows` list
- Body: page summary paragraph, section tables with descriptions filled in, Mermaid flowchart if flows exist

### 5 — Commit and push

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

**On flowcharts:** Screenshots don't tell you flows. The Figma API node tree includes a `reactions` field on each frame — these are the prototype arrows (click/hover interactions) connecting frames. That's the authoritative source. `figmaclaw enrich` extracts these automatically. The LLM supplements with *inferred* flows from name patterns (e.g., `default` → `hover` → `active`). If a page has no prototype reactions, no Mermaid block is generated — that's correct behavior, not a gap.

## Notes

- The `reserach` section typo in some Figma files is real — preserve it as-is so the node_id mapping stays correct
- Small frames (≤200px) inside sections are usually icon/component details, not full screens
- If the page has no flows at all, omit the `flows` key and the Mermaid block entirely
