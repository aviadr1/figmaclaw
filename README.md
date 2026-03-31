# figmaclaw

Figma → git semantic design memory for AI agents.

Mirrors Figma pages as AI-readable markdown maps stored in git. Each `.md` file is a navigation index for one Figma page: screen inventory, semantic descriptions, node IDs, and flow diagrams — so an AI can find and edit the right screen without re-reading raw Figma every time.

## How it works

```
Designer saves Figma
  → Figma FILE_UPDATE webhook → CloudFlare Worker
  → GitHub repository_dispatch
  → figmaclaw apply-webhook
  → re-fetches from Figma API (never uses stale payload)
  → page structural hash check → only regenerate changed pages
  → commit to repo
```

Plus nightly `figmaclaw pull` for reconciliation.

## Output format

One `.md` per Figma page, stored at `figma/{file-key}/pages/{page-slug}.md`.

**Policy: all structured data needed by machines lives in the YAML frontmatter.** The frontmatter schema is enforced by `FigmaPageFrontmatter` (Pydantic). The body (tables, Mermaid chart, prose) is a rendered view for human and AI reading only — never parse table rows or prose to extract node IDs, descriptions, or flows.

| Field | Location | Purpose |
|---|---|---|
| `file_key` | frontmatter | Figma file key for API calls |
| `page_node_id` | frontmatter | Figma CANVAS node ID |
| `frames` | frontmatter (inline dict) | node_id → description, authoritative source |
| `flows` | frontmatter (inline list) | `[[from_id, to_id], ...]` prototype edges |

```markdown
---
file_key: hOV4QMBnDIG5s5OYkSrX9E
page_node_id: "7741:45837"
frames: {"10676:5534": "Empty state – no socials connected yet", "10705:6560": "Full list of connected platforms"}
flows: [["10706:9231", "10639:4378"]]
---

# Web App / Reach — Auto Content Sharing

**File:** Web App · [Open in Figma](https://www.figma.com/design/...)

## manage accounts (`10706:9231`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| no account connected | `10676:5534` | Empty state – no socials connected yet |
...

## Quick Reference

| Screen | Node ID | Section | Description |
...
```

## Install

```bash
uv tool install git+https://github.com/aviadr1/figmaclaw
```

Or for development:

```bash
git clone https://github.com/aviadr1/figmaclaw
cd figmaclaw
./install.sh
```

## Quickstart

```bash
# Set required env vars
export FIGMA_API_KEY=figd_...

# Track a Figma file (run initial pull)
figmaclaw track hOV4QMBnDIG5s5OYkSrX9E

# Pull all tracked files (incremental)
figmaclaw pull

# Force full regeneration
figmaclaw pull --force

# Set up CI/CD (copies workflow files, registers webhook)
figmaclaw init
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `FIGMA_API_KEY` | Yes | Figma personal access token (`figd_...`) |
| `FIGMA_WEBHOOK_SECRET` | Webhook only | Passcode for webhook validation |

## Architecture

- **Separate repo from issueclaw** — zero shared code, independently installable
- **Read-only** — never writes back to Figma
- **Incremental** — three-level short-circuit: file version → page hash → frame-level description preservation
- **Pydantic everywhere** — all models validated at boundary, no naked dicts
- **`X-Figma-Token` header** — Figma's auth is not `Authorization: Bearer`

## Relationship to issueclaw

figmaclaw follows the same architecture as [issueclaw](https://github.com/aviadr1/issueclaw) (webhook → fetch → render → commit) but is a completely separate tool for a different source system. Both write to the same git repo but to different directories (`figma/` and `.figma-sync/` vs `linear/` and `.sync/`).
