# figmaclaw

Figma → git semantic design memory for AI agents.

Mirrors Figma pages as AI-readable markdown maps stored in git. Each `.md` file has YAML frontmatter (machine-readable: frame IDs, flows, enrichment state) and a prose body (LLM-written: descriptions, summaries, Mermaid flowcharts). AI agents can find and understand any Figma screen without re-reading raw Figma.

## How it works

```
Designer saves in Figma
  → webhook / hourly cron
  → figmaclaw pull (fetches Figma API, updates frontmatter)
  → figmaclaw claude-run (LLM enrichment — optional)
      → inspect: what needs descriptions?
      → screenshots --stale: download only changed frames
      → Claude writes descriptions via write-body
      → mark-enriched: record what's done
  → git commit + push
```

Three levels of incremental sync avoid unnecessary work:

1. **File level** — skip if Figma version unchanged (1 cheap API call)
2. **Page level** — skip if structural hash unchanged (no page fetch)
3. **Frame level** — re-enrich only frames whose content hash changed ($0.10 vs $15 for a 500-frame page)

## Install

```bash
# Install (requires uv)
curl -fsSL https://raw.githubusercontent.com/aviadr1/figmaclaw/main/install.sh | sh

# Or manually
uv tool install git+https://github.com/aviadr1/figmaclaw

# Upgrade
uv tool install --force git+https://github.com/aviadr1/figmaclaw
```

See [docs/INSTALL.md](docs/INSTALL.md) for full setup guide including CI/CD and webhooks.

## Quick start

```bash
export FIGMA_API_KEY=figd_...

# Track a Figma file
figmaclaw track <file-key>

# Pull all tracked files (incremental)
figmaclaw pull

# Check enrichment status
figmaclaw inspect figma/<file>/pages/<page>.md --json

# Set up CI/CD (copies workflow templates)
figmaclaw init

# Verify installation works
figmaclaw doctor
```

## 5-minute local run

```bash
# 1) Track a file once
figmaclaw track <file-key>

# 2) Pull latest Figma metadata into markdown
figmaclaw pull

# 3) Inspect one page and see what needs enrichment
figmaclaw inspect figma/<file>/pages/<page>.md --json

# 4) (Optional) download stale frame screenshots for enrichment
figmaclaw screenshots --stale figma/<file>/pages/<page>.md
```

## Output format

One `.md` per Figma page at `figma/{file-slug}/pages/{page-slug}.md`:

```yaml
---
file_key: abc123
page_node_id: '7741:45837'
frames: ['11:1', '11:2', '46:42']
flows: [['11:1', '11:2']]
enriched_hash: b39103d8
enriched_frame_hashes: {'11:1': 'a3f2b7c1', '11:2': 'e4d9f8a2'}
raw_frames: {'11:1': {raw: 3, ds: [ButtonV2, AvatarV2]}}
---

# Web App / Onboarding

2-3 sentence page summary...

## Login (`10:1`)

One-sentence section intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Welcome | `11:1` | Welcome screen with email input and Sign In button. |
| Permissions | `11:2` | Camera access prompt with allow/deny options. |

## Screen flows

```mermaid
flowchart LR
    n11_1["Welcome"] --> n11_2["Permissions"]
```​
```

- **Frontmatter** = machine-readable source of truth. Frame IDs, flows, enrichment state.
- **Body** = LLM-written prose. Never parsed by code. Never mechanically rewritten.

Full spec: [docs/figmaclaw-md-format.md](docs/figmaclaw-md-format.md)

## Commands

| Command | What it does | Requires API? |
|---|---|---|
| `track` | Register a Figma file for syncing | Figma |
| `pull` | Bulk sync all tracked files | Figma |
| `sync` | Re-sync a single .md file | Figma |
| `inspect` | Check page structure + enrichment state | No |
| `screenshots` | Download frame PNGs | Figma |
| `write-body` | Write prose body (preserves frontmatter) | No |
| `mark-enriched` | Record enrichment state | No |
| `mark-stale` | Force re-enrichment | No |
| `set-flows` | Write prototype flows to frontmatter | No |
| `claude-run` | Orchestrate LLM enrichment in CI | Claude |
| `stream-format` | Format stream-json for CI logs | No |
| `init` | Copy workflow templates to consumer repo | No |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `FIGMA_API_KEY` | Yes | Figma personal access token (`figd_...`) |
| `FIGMA_WEBHOOK_SECRET` | Webhooks | Passcode for webhook validation |
| `FIGMA_TEAM_ID` | Auto-discovery | Figma team ID for `figmaclaw list` |
| `CLAUDE_CODE_OAUTH_TOKEN` | CI enrichment | Claude Code CLI auth for LLM descriptions |

## Architecture

- **Read-only** — never writes back to Figma
- **Zero LLM dependency** for sync — enrichment is a separate optional step
- **Incremental** — three-level short-circuit: file version → page hash → per-frame content hash
- **Pydantic everywhere** — all models validated, no naked dicts
- **Reusable workflows** — consumer repos call upstream `.github/workflows/` with repo-specific config

## Relationship to issueclaw

figmaclaw follows the same architecture as [issueclaw](https://github.com/aviadr1/issueclaw) (webhook → fetch → render → commit) but is a separate tool for Figma. Both write to the same consumer repo but to different directories (`figma/` vs `linear/`).

## Development

```bash
git clone https://github.com/aviadr1/figmaclaw
cd figmaclaw
./install.sh                    # uv sync + pre-commit install

# local quality checks (same gates as CI)
uv run ruff format --check .
uv run ruff check .
uv run --group dev python -m basedpyright
uv run python -m pytest -q --cov=figmaclaw --cov-report=term-missing --cov-fail-under=70
```

## CI quality gates

Pull requests and pushes to `main` run:

- `ruff format --check`
- `ruff check`
- `basedpyright`
- `pytest` with coverage gate (`--cov-fail-under=70`)
- JUnit test report publishing (GitHub checks annotations)
- Coverage XML artifact upload (and Codecov upload when available)

All four quality jobs are expected to be blocking in branch protection.

## Troubleshooting

- `MCP initialize has no Mcp-Session-Id`: this is supported. `FigmaMcpClient` handles both sessionful and stateless MCP responses.
- `No Figma token found`: set `FIGMA_MCP_TOKEN` or authenticate Figma in Claude Code so `~/.claude/.credentials.json` has `mcpOAuth` token data.
- `pre-commit` not found locally: run `uv run --with pre-commit python -m pre_commit install`.
