---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and inferred screen flows.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with:
- **Frame descriptions** — 1–3 sentence description for every frame
- **Page summary** — 2–3 sentence prose overview of the whole page
- **Section intros** — one sentence per section describing what that group shows
- **Mermaid flowchart** — screen-flow diagram showing how screens connect (always present)

**Format spec:** [`docs/figmaclaw-md-format.md`](https://github.com/aviadr1/figmaclaw/blob/main/docs/figmaclaw-md-format.md) — authoritative reference for frontmatter schema, body structure, and known limitations.

## Design contract

**Frontmatter = machine-readable source of truth.** CI reads and writes this. Use it to know WHAT needs updating — which frames exist, which have descriptions, what the flows are.

**Body = human/LLM prose only.** Never parsed by code. The correct update path is always:
1. Read the **existing** `.md` file verbatim (body included)
2. Fetch **new Figma data** via frontmatter (screenshots, updated structure)
3. Pass **both** to the LLM: existing body + new data
4. LLM rewrites the body — preserving existing prose where still accurate, adding what's missing

## Expected output

```markdown
---
file_key: abc123
frames: {'node1': 'Description of frame 1.', 'node2': 'Description of frame 2.'}
flows: [['node1', 'node2']]
page_node_id: '1234:5678'
---

# File Name / Page Name

[Open in Figma](https://www.figma.com/design/abc123?node-id=1234-5678)

This page covers the onboarding flow across three steps: email entry, OTP verification,
and profile completion. Each section shows both the default and error states for each step.

## Section name (`section_node_id`)

The default and error states for email entry.

| Screen | Node ID | Description |
|--------|---------|-------------|
| screen name | `node1` | Description of frame 1. |
| screen name | `node2` | Description of frame 2. |

## Screen flows

```mermaid
flowchart LR
    A["screen name"] -->|user taps Continue| B["next screen"]
` `` `
```

Body placement rules:
- **Page summary** — immediately after the `[Open in Figma]` link, before the first `##`
- **Section intro** — one sentence immediately after each `##` heading, before the table
- **Mermaid block** — always present, at the very end of the file under `## Screen flows`

## Workflow (figmaclaw CLI available — e.g. CI)

### Step 1 — Read the existing file

```bash
cat <file_path>
```

Note the full body verbatim — you will pass it to the LLM in Step 7. Identify from frontmatter:
- Which frames have no description yet (empty string or missing from `frames:`)
- Whether `flows:` is present and non-empty
- Total frame count

### Step 2 — Re-sync structure from Figma

```bash
figmaclaw enrich <file_path>
```

Fetches the latest page structure, rebuilds sections/frames tables, extracts prototype `reactions`
into the `flows:` frontmatter field. **Does NOT generate descriptions.** Preserves existing ones.

### Step 3 — Check what's missing

```bash
figmaclaw page-tree <file_path> --json --missing-only
```

Exit code 1 = frames missing (normal at this point). Exit code 0 = all described, skip to Step 7.

### Step 4 — Download screenshots

```bash
figmaclaw screenshots <file_path> --pending
```

Downloads PNGs to `.figma-cache/screenshots/<file_key>/` for frames without descriptions.
Outputs a JSON manifest: `{file_key, screenshots: [{node_id, path}]}`.

If `## Screen flows` is missing from the body (even when all frames are already described),
download **all** screenshots instead:

```bash
figmaclaw screenshots <file_path>
```

You need to see the actual design to understand what connects to what — buttons, modals, step
indicators, and CTAs show the flow. Do not guess from frame names.

### Step 5 — Generate descriptions via subagents

Process in **batches of 8** — spawn a subagent per batch so screenshots leave the main context:

> "Here are N Figma frames. Read each PNG with the Read tool:
> `<path_1>` (node_id: `<node_id_1>`), `<path_2>` (node_id: `<node_id_2>`), ...
>
> For each frame write a description of 1–3 sentences:
> - What the screen shows and its current state
> - Key UI elements visible (inputs, modals, CTAs, overlays, toggles, etc.)
> - What makes it visually distinct from its siblings
>
> Return only a JSON object: `{ "<node_id>": "<description>", ... }`"

While viewing screenshots, also form an understanding of the overall page — you'll need it for Step 7.

### Step 6 — Write descriptions to frontmatter

Pipe JSON to stdin — **never use `--frames`** (descriptions may contain single quotes):

```bash
figmaclaw set-frames <file_path> << 'EOF'
{
  "node_id_1": "Description text, can contain 'single quotes' freely.",
  "node_id_2": "Another description."
}
EOF
```

### Step 7 — Rewrite the body

Read the current file (frontmatter now has all descriptions and flows). Then rewrite the body:

- **Page summary** — if missing or placeholder, write 2–3 sentences: what this page covers, its purpose, what differentiates the main sections. Insert after `[Open in Figma]` link, before first `##`.
- **Section intros** — if missing, add one sentence after each `##` heading describing what that group of frames shows.
- **Mermaid flowchart** — always include. If `## Screen flows` doesn't exist yet, append it at the end. Build the graph from:
  1. `flows:` frontmatter (authoritative Figma prototype reactions) — use these edges first
  2. **Look at the screenshots** — the visual design shows what leads where. Buttons, arrows, modal triggers, step indicators, and CTAs all tell you how screens connect. Do not guess from frame names alone.

```markdown
## Screen flows

```mermaid
flowchart LR
    A["<frame name>"] -->|<action inferred from design>| B["<next frame name>"]
` `` `
```

  Use frame names from the body tables as node labels (not raw node IDs). Label transitions with the actual user action visible in the design (e.g. "taps Go Live button", "submits OTP", "payment complete").

- **Preserve existing prose** — if a page summary, section intros, or Mermaid block already exist
  and are still accurate, keep them. Only update what's wrong or missing.

Use the Edit tool to make targeted changes to the file (don't rewrite the entire file from scratch
unless it was previously empty).

### Step 8 — Verify

```bash
figmaclaw page-tree <file_path> --json
```

Confirm `missing_descriptions` is 0.

### Step 9 — Commit and push

```bash
git add <file_path> .figma-sync/
git commit -m "sync: enrich <page-name> with frame descriptions"
git push || (git pull --no-rebase && git push)
```

## Workflow (MCP available — no CLI)

### 1 — Read the existing file

Use the Read tool. Note the full body and frontmatter.

### 2 — Get screenshots via MCP in batches of 8

For each batch, spawn a subagent:

> "Here are 8 Figma frames from file `<file_key>`. Call `get_screenshot` for all 8 node IDs **in parallel**:
> `<node_id_1>`, ... `<node_id_8>`
>
> Return only a JSON object: `{ "<node_id>": "<1–3 sentence description>", ... }`"

### 3 — Write descriptions to frontmatter

```bash
figmaclaw set-frames <file_path> << 'EOF'
{ ... }
EOF
```

Always use stdin heredoc — never `--frames`.

### 4 — Rewrite the body

See Step 7 above.

## What to fill and where to get it

| What | Source |
|---|---|
| Frame descriptions | Screenshots — CLI: `figmaclaw screenshots` + Read tool; MCP: `get_screenshot` |
| Page summary | Screenshots + overall understanding of the page |
| Section intros | Screenshots + what each frame group has in common |
| Mermaid flowchart | Always present. `flows:` frontmatter as authoritative edges; look at screenshots to find additional transitions visible in the design |

## Notes

- **Never parse body prose** for node IDs, descriptions, or flows — always read frontmatter
- The `reserach` section typo in some Figma files is real — preserve it as-is
- Small frames (≤200px) inside sections are usually icon/component details — describe them, note they are components
- If a section's table is empty after `figmaclaw enrich`, those frames had no node IDs in Figma — skip it
- Always include `## Screen flows` — humans can't read frontmatter YAML. Look at the screenshots to understand what connects to what. Buttons, arrows, step indicators, and CTAs in the design show the flow.
