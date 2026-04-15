# Installing figmaclaw

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Install the CLI

```bash
uv tool install git+https://github.com/aviadr1/figmaclaw
```

Verify: `figmaclaw --version`

Upgrade: `uv tool install --force git+https://github.com/aviadr1/figmaclaw`

## Environment variables

| Variable | When needed | Description |
|---|---|---|
| `FIGMA_API_KEY` | Always | Figma personal access token. Generate at https://www.figma.com/developers/api#access-tokens (format: `figd_...`) |
| `FIGMA_MCP_TOKEN` | MCP smoke tests | Figma MCP OAuth access token used by `smoke_mcp` and `figmaclaw/figma_mcp.py` |
| `FIGMA_WEBHOOK_SECRET` | Webhooks | Passcode for validating webhook payloads |
| `FIGMA_TEAM_ID` | Auto-discovery | Your Figma team ID (for `figmaclaw list`) |
| `CLAUDE_CODE_OAUTH_TOKEN` | CI enrichment | Claude Code CLI auth for LLM-powered description generation |

Set them in your shell or as GitHub Actions secrets (see CI setup below).

Token creation, lifetime, and rotation details: [docs/token-auth-and-rotation.md](token-auth-and-rotation.md)

## Quick start — local

```bash
export FIGMA_API_KEY=figd_...

# Track a Figma file
figmaclaw track <file-key>

# Pull all tracked files (incremental — skips unchanged pages)
figmaclaw pull

# Check enrichment status of a page
figmaclaw inspect figma/<file>/pages/<page>.md --json

# Force full re-sync
figmaclaw pull --force
```

The file key is the ID in the Figma URL: `figma.com/design/<file-key>/...`

## Quick start — CI/CD (consumer repo)

This sets up automated sync so your repo stays current with Figma.

### 1. Initialize

```bash
cd /path/to/your-repo
figmaclaw init
```

This copies workflow templates to `.github/workflows/`:
- `figmaclaw-sync.yaml` — hourly cron sync + LLM enrichment
- `figmaclaw-webhook.yaml` — real-time webhook sync
- `figmaclaw-manage-webhooks.yaml` — idempotent webhook registration/repair workflow

### 2. Set GitHub secrets

In your repo's Settings > Secrets and variables > Actions:

| Secret | Value |
|---|---|
| `FIGMA_API_KEY` | Your Figma personal access token |
| `FIGMA_WEBHOOK_SECRET` | Passcode for webhook validation |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token (for enrichment) |

And under Variables:

| Variable | Value |
|---|---|
| `FIGMA_TEAM_ID` | Your Figma team ID |

### 3. Set up webhooks (optional, for real-time sync)

Figma webhooks require a publicly accessible endpoint. Deploy the included CloudFlare Worker:

```bash
cd workers/figmaclaw-webhook-proxy/
# Set secrets in wrangler.toml or via wrangler CLI
wrangler secret put FIGMA_WEBHOOK_SECRET
wrangler secret put GITHUB_TOKEN
wrangler secret put GITHUB_REPO   # e.g. "myorg/my-design-repo"
wrangler deploy
```

Then run the webhook-management workflow (`figmaclaw-manage-webhooks.yaml`) with
your worker endpoint to register/update webhooks for tracked files.

Without webhooks, the hourly cron sync still keeps the repo up to date.

### 4. Track files and do initial pull

```bash
export FIGMA_API_KEY=figd_...

# Auto-discover and track all files in your team
figmaclaw list <team-id> --track-only

# Pull everything
figmaclaw pull
git add figma/ .figma-sync/
git commit -m "initial figmaclaw sync"
git push
```

After this, CI takes over — the hourly cron and webhooks keep the repo current.

## How CI works

```
Figma webhook (or hourly cron)
  |
  v
figmaclaw pull / apply-webhook     (fetches Figma API, updates frontmatter)
  |
  v
figmaclaw claude-run               (LLM enrichment — optional)
  |-- inspect: checks which pages need descriptions
  |-- screenshots --stale: downloads only changed frames
  |-- Claude writes descriptions via write-body
  |-- mark-enriched: records enrichment state
  |
  v
git commit + push
```

The enrichment step is optional — without `CLAUDE_CODE_OAUTH_TOKEN`, sync still works but pages won't have LLM-generated descriptions.

## Development setup

```bash
git clone https://github.com/aviadr1/figmaclaw
cd figmaclaw
./install.sh          # runs: uv sync + pre-commit install

# Run tests
uv run pytest

# Type check
uv run basedpyright

# Lint
uv run ruff check && uv run ruff format --check
```

## Verify installation

```bash
# Quick check — reports what's configured and what's missing
figmaclaw doctor

# Full installation test (creates a temp repo, installs, inits, validates)
bash tests/test_install_e2e.sh

# Full test including Figma API connectivity
FIGMA_API_KEY=figd_... bash tests/test_install_e2e.sh --full
```

## Not on PyPI (yet)

figmaclaw is installed from GitHub. Consumer repos don't add it to `pyproject.toml` — CI installs it fresh each run via `uv tool install git+https://github.com/aviadr1/figmaclaw`.
