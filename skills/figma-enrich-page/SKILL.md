---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and screen flows from design inspection.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with:
- **Frame descriptions** — 1–3 sentence description for every frame
- **Page summary** — 2–3 sentence prose overview of the whole page
- **Section intros** — one sentence per section describing what that group shows
- **Mermaid flowchart** — always present; built from `flows:` frontmatter (authoritative Figma reactions) and design inspection via screenshots

**Full workflow:** see `figmaclaw/skills/figma-enrich-page.md` (the authoritative skill file).
Run `figmaclaw self skill enrich-page` to print it at runtime.

## Design contract

**Frontmatter = machine-readable source of truth.** CI reads and writes this. Use it to know WHAT needs updating — which frames exist, which have descriptions, what the flows are.

**Body = human/LLM prose only.** Never parsed by code. The correct update path is always:
1. Read the **existing** `.md` file verbatim (body included)
2. Fetch **new Figma data** via frontmatter (screenshots, updated structure)
3. Pass **both** to the LLM: existing body + new data
4. LLM rewrites the body — preserving existing prose where still accurate, adding what's missing

Use `figmaclaw sync` to re-sync frontmatter from Figma without touching the body.
Use `figmaclaw sync --scaffold` to print the scaffold template as a structural hint.
Use `figmaclaw sync --show-body` to print the existing body for the LLM to preserve.

## When to use

- A page under `figma/*/pages/` has `(no description yet)` placeholders
- A page's structure changed in Figma and descriptions need updating
- You want to refresh a specific page

## Quick summary (CLI path)

```bash
# 1. Read existing file — note full body verbatim
cat <file_path>

# 2. Check what needs descriptions
figmaclaw page-tree <file_path> --json --missing-only

# 3. Download screenshots for pending frames
figmaclaw screenshots <file_path> --pending

# 4. Generate descriptions via LLM subagents (batches of 8)

# 5. Write descriptions to frontmatter
figmaclaw set-frames <file_path> << 'EOF'
{ "node_id": "description" }
EOF

# 6. LLM rewrites body: preserves existing summary/intros, updates tables and Mermaid

# 7. Commit and push
git add <file_path> .figma-sync/
git commit -m "sync: describe <page-name> frames"
git push || (git pull --no-rebase && git push)
```

For the full step-by-step workflow with subagent prompts, body format rules, and Mermaid format, run:

```bash
figmaclaw self skill enrich-page
```

## Notes

- **Never parse body prose** for node IDs, descriptions, or flows — always read frontmatter
- The `reserach` section typo in some Figma files is real — preserve it as-is
- Small frames (≤200px) are usually component details — describe them, note they are components
- Always include `## Screen flows` — look at screenshots to find transitions visible in the design (buttons, CTAs, step indicators, modals). Use `flows:` frontmatter as authoritative edges but don't rely on it alone. Never guess from frame names.
