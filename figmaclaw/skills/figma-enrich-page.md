---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and inferred screen flows.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with descriptions for every frame, a page summary, and inferred screen flow edges.

**Format spec:** [`docs/figmaclaw-md-format.md`](../../../docs/figmaclaw-md-format.md) — read this first if you are unsure about frontmatter fields, body structure, what is authoritative, or known limitations.

## Design contract — the only correct update path

**Frontmatter = machine-readable source of truth.** It tells you WHAT needs updating: which frames exist, which have descriptions, which are new since the last run.

**Body = human/LLM prose.** It is never parsed by code. The only correct way to update a page is:

1. Read the **existing** `.md` file (body included, verbatim)
2. Fetch **new Figma data** (frame screenshots, updated structure) — guided by the frontmatter
3. Pass **both** to the LLM: existing body + what changed
4. The LLM rewrites the body, preserving page summary and section intros where still accurate, updating descriptions for changed/new frames

This is the path used both by agents running this skill manually AND by CI/hourly hooks. There is no "CLI-only no-LLM" path for pages that already have prose.

## When to use

- A page under `figma/*/pages/` has `(no description yet)` placeholders
- A page's structure changed in Figma and descriptions need updating
- You want to refresh descriptions for a specific page

## Step-by-step (figmaclaw installed, MCP unavailable — e.g. CI)

### 1 — Read the existing file

```bash
cat figma/<file>/<page>.md
```

Note the full body — you will pass it to the LLM later. Use the frontmatter to identify:
- Which frames have no description yet (`(no description yet)` in body tables or missing from `frames:`)
- Total frame inventory

### 2 — Inspect the page for missing descriptions

```bash
figmaclaw page-tree figma/<file>/<page>.md --json --missing-only
```

Exit code 1 = frames missing (normal); 0 = all done.

### 3 — Download screenshots for frames that need updating

```bash
figmaclaw screenshots figma/<file>/<page>.md --pending
```

Downloads PNGs to `.figma-cache/screenshots/<file_key>/` for all frames without descriptions yet.
Outputs a JSON manifest: `{file_key, screenshots: [{node_id, path}]}`.

`--pending` skips frames that already have descriptions in the frontmatter.

### 4 — Generate descriptions via LLM

Read each PNG with the Read tool (Claude can see images). For each frame write a description of 1–3 sentences:
- What the screen shows and its current state
- Key UI elements visible (inputs, modals, CTAs, overlays, toggles, etc.)
- What makes it visually distinct from its siblings

Process in batches via subagents to keep screenshots out of the main context.

### 5 — Write descriptions to frontmatter

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

### 6 — Rewrite the body via LLM

Now give the LLM:
- The **existing body** of the `.md` file (verbatim — all prose, tables, Mermaid)
- The **updated frontmatter** (with all descriptions now filled in)
- This **instruction:**

> "Rewrite the body of this figmaclaw page file. Keep the exact H1 title, Figma URL link, and frontmatter unchanged. For the body:
> - Write or improve the page summary paragraph (what this Figma page covers, its purpose)
> - For each section, write or improve the intro sentence (what this section shows)
> - Update the frame description table rows to match the descriptions in the frontmatter
> - Keep the Mermaid flowchart if flows exist, remove it if empty
> - If there was already a page summary or section intros in the existing body, preserve them unless the page structure changed significantly enough to warrant updating them
> - Never invent node IDs or frame names — copy them exactly from the existing tables"

The LLM produces the final `.md`. Write it back to disk.

### 7 — Commit and push

```bash
git add figma/<file>/<page>.md
git commit -m "sync: enrich <page-name> with descriptions"
git push
```

## Step-by-step (MCP available — agent with Figma MCP)

### 1 — Read the existing file

Use the Read tool to get the full `.md`. Note the existing body — you will preserve it.

### 2 — Get screenshots via MCP in batches of 8

For each batch, spawn a subagent:

> "Here are 8 Figma frames from file `<file_key>`. Call `get_screenshot` for all 8 node IDs **in parallel**:
> `<node_id_1>`, ... `<node_id_8>`
>
> Return only a JSON object: `{ "<node_id>": "<1–3 sentence description>", ... }`"

### 3 — Write descriptions to frontmatter via stdin

See step 5 above — always use stdin heredoc, never `--frames`.

### 4 — Rewrite the body

See step 6 above — pass existing body + updated frontmatter to LLM.

## What information you need from a page

| What to fill | Source |
|---|---|
| Frame descriptions | Screenshots per frame — CLI: `figmaclaw screenshots` + Read tool; MCP: `get_screenshot` / `get_design_context` |
| Page summary | Screenshots + understanding of the whole page + existing body prose |
| Section intros | Screenshots + existing body prose |
| Flowchart edges | `reactions` in Figma node tree — extracted by `figmaclaw page-tree` / frontmatter `flows:` |

**On frame names:** Screenshots are still needed even when names are descriptive. Names tell you the state variant but not layout, visible UI elements, or whether the name is accurate.

**On flowcharts:** The Figma API `reactions` field is authoritative. The frontmatter `flows:` key is the source of truth. No flows → no Mermaid block.

**On preserving prose:** The existing body is the memory of previous enrichment. Always start from it — don't regenerate from scratch. The LLM should update what changed, not erase what's already good.

## Notes

- The `reserach` section typo in some Figma files is real — preserve it as-is so the node_id mapping stays correct
- Small frames (≤200px) inside sections are usually icon/component details, not full screens
- If the page has no flows at all, omit the `flows` key and the Mermaid block entirely
