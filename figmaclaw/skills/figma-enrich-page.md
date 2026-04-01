---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and inferred screen flows.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with descriptions for every frame, a page summary, and inferred screen flow edges.

**Format spec:** [`docs/figmaclaw-md-format.md`](../../../docs/figmaclaw-md-format.md) — read this first if you are unsure about frontmatter fields, body structure, what is authoritative, or known limitations.

## When to use

- A page under `figma/*/pages/` has `(no description yet)` placeholders
- You want to re-sync descriptions for a specific page without a full pull

## Preferred approach — use the CLI (figmaclaw installed)

```bash
figmaclaw enrich figma/web-app/pages/<page-slug>.md [--auto-commit]
```

Fetches the current page structure from Figma, merges existing descriptions, rewrites the file, and updates the manifest. Does not call an LLM — use the manual steps below to generate descriptions.

### What it reads from Figma
- `GET /v1/files/{file_key}?depth=1` — file name and version
- `GET /v1/files/{file_key}/nodes?ids={page_node_id}` — full page node tree

## Manual steps (figmaclaw installed, MCP unavailable — e.g. CI)

### 1 — Inspect the page

```bash
figmaclaw page-tree figma/<file>/<page>.md --json --missing-only
```

This prints every frame that needs a description. Exit code 1 = frames missing (normal); 0 = all done.

### 2 — Download screenshots

```bash
figmaclaw screenshots figma/<file>/<page>.md --pending
```

Downloads PNGs to `.figma-cache/screenshots/<file_key>/` for all frames without descriptions yet.
Outputs a JSON manifest: `{file_key, screenshots: [{node_id, path}]}`.

`--pending` skips frames that already have descriptions.

### 3 — Generate descriptions

Read each PNG with the Read tool (Claude can see images). For each frame write a description of 1–3 sentences:
- What the screen shows and its current state
- Key UI elements visible (inputs, modals, CTAs, overlays, toggles, etc.)
- What makes it visually distinct from its siblings

Process in batches via subagents to keep screenshots out of the main context.

### 4 — Write descriptions

Pipe JSON to stdin — **do not use `--frames`** as descriptions may contain single quotes that break shell quoting:

```bash
figmaclaw set-frames figma/<file>/<page>.md << 'EOF'
{
  "node_id_1": "Description text, can contain 'single quotes' freely.",
  "node_id_2": "Another description."
}
EOF
```

`set-frames` merges new descriptions into the frontmatter `frames:` dict. Existing descriptions are preserved.

### 5 — Commit and push

```bash
git add figma/<file>/<page>.md
git commit -m "sync: enrich <page-name> with descriptions"
git push
```

## Fallback — manual steps (figmaclaw not installed, MCP available)

### 1 — Read the existing .md for the frame inventory

Frontmatter contains `file_key` and `page_node_id`. Body tables list every frame with its node ID.

### 2 — Get screenshots via MCP in batches of 8

For each batch, spawn a subagent:

> "Here are 8 Figma frames from file `<file_key>`. Call `get_screenshot` for all 8 node IDs **in parallel**:
> `<node_id_1>`, ... `<node_id_8>`
>
> Return only a JSON object: `{ "<node_id>": "<1–3 sentence description>", ... }`"

### 3 — Write with set-frames via stdin

See step 4 above — always use stdin heredoc, never `--frames`.

## What information you need from a page

| What to fill | Source |
|---|---|
| Frame descriptions | Screenshots per frame — CLI: `figmaclaw screenshots` + Read tool; MCP: `get_screenshot` / `get_design_context` |
| Page summary | Screenshots + understanding of the whole page |
| Flowchart edges | `reactions` in Figma node tree — fetched automatically by `figmaclaw enrich` |

**On frame names:** Screenshots are still needed even when names are descriptive. Names tell you the state variant but not layout, visible UI elements, or whether the name is accurate.

**On flowcharts:** The Figma API `reactions` field is authoritative. `figmaclaw enrich` extracts these automatically. No reactions → no Mermaid block (correct behavior).

## Notes

- The `reserach` section typo in some Figma files is real — preserve it as-is so the node_id mapping stays correct
- Small frames (≤200px) inside sections are usually icon/component details, not full screens
- If the page has no flows at all, omit the `flows` key and the Mermaid block entirely
