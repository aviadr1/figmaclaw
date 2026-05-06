---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and screen flows from design inspection.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with:
- **Frame descriptions** — 1–3 sentence description for every frame
- **Page summary** — 2–3 sentence prose overview of the whole page
- **Section intros** — one sentence per section describing what that group shows
- **Mermaid flowchart** — built from `flows:` frontmatter and design inspection via screenshots

**Format spec:** [`docs/figmaclaw-md-format.md`](https://github.com/aviadr1/figmaclaw/blob/main/docs/figmaclaw-md-format.md)

## Design contract

**Frontmatter = machine-readable metadata.** Tracks what exists (`frames:` as ID list), how screens connect (`flows:`), and whether the body is stale (`enriched_hash`). No descriptions in frontmatter.

**Body = human/LLM prose only.** Page summary, section intros, description tables, Mermaid charts. Never parsed by code. Only written by LLM via `write-body`.

## Workflow

### Step 1 — Check what needs enrichment

```bash
figmaclaw inspect <file_path> --json
```

Check `needs_enrichment` in the JSON output. If false, skip this file.

### Step 2 — Read the existing file

```bash
cat <file_path>
```

Note the full body verbatim — you will preserve and adapt it.

### Step 3 — Download screenshots

```bash
figmaclaw screenshots <file_path> --stale
```

Downloads PNGs to `.figma-cache/screenshots/<file_key>/` for frames without descriptions.
If `## Screen flows` is missing from the body, download **all** screenshots:

```bash
figmaclaw screenshots <file_path>
```

### Step 4 — Generate descriptions via subagents

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

### Step 5 — Write the body

Use `figmaclaw write-body` to write the complete body:

```bash
figmaclaw write-body <file_path> <<'EOF'

# {file_name} / {page_name}

[Open in Figma]({figma_url})

{2-3 sentence page summary}

## {Section Name} (`{section_node_id}`)

{1-sentence section intro}

| Screen | Node ID | Description |
|--------|---------|-------------|
| {frame_name} | `{node_id}` | {description from step 4} |

## Screen flows

```mermaid
flowchart LR
    A["screen"] -->|action from design| B["next screen"]
` ``
EOF
```

Body placement rules:
- **Page summary** — immediately after the `[Open in Figma]` link, before the first `##`
- **Section intro** — one sentence immediately after each `##` heading, before the table
- **Mermaid block** — always present, at the very end under `## Screen flows`

Look at screenshots for transitions — buttons, CTAs, step indicators, modals show the flow. Don't guess from frame names alone.

**Preserve existing prose** — if a page summary, section intros, or Mermaid block already exist and are still accurate, keep them.

### Step 6 — Set flows (if inferred)

If you identified flows from screenshots that aren't already in frontmatter:

```bash
figmaclaw set-flows <file_path> --flows '[["src_id", "dst_id"], ...]'
```

### Step 7 — Mark as enriched

```bash
figmaclaw mark-enriched <file_path>
```

This snapshots the current page hash and frame hashes into frontmatter, so the system knows the body is up-to-date.

### Step 8 — Verify

```bash
figmaclaw inspect <file_path> --json
```

Check `needs_enrichment` is false.

### Step 9 — Commit and push

```bash
git add <file_path>
git commit -m "sync: describe <page-name> frames"
git push
```

If push is rejected, stop and report the rejected push. Do not merge or rewrite generated artifacts as recovery.

## Notes

- **Never parse body prose** for node IDs, descriptions, or flows — always read frontmatter
- The `reserach` section typo in some Figma files is real — preserve it as-is
- Small frames (≤200px) inside sections are usually icon/component details — describe them, note they are components
- Always include `## Screen flows` — look at screenshots to find transitions visible in the design
- **No `set-frames`** — descriptions live in the body, not frontmatter. Use `write-body` instead.
