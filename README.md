# figmaclaw

[![CI (main)](https://github.com/aviadr1/figmaclaw/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/aviadr1/figmaclaw/actions/workflows/ci.yml?query=branch%3Amain)
[![CodeQL (main)](https://github.com/aviadr1/figmaclaw/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/aviadr1/figmaclaw/actions/workflows/codeql.yml?query=branch%3Amain)
[![Coverage (main)](https://codecov.io/gh/aviadr1/figmaclaw/branch/main/graph/badge.svg)](https://app.codecov.io/gh/aviadr1/figmaclaw/tree/main)
[![Ruff](https://img.shields.io/badge/lint-ruff-46a2f1?logo=ruff&logoColor=white)](https://docs.astral.sh/ruff/)
[![Basedpyright](https://img.shields.io/badge/types-basedpyright-5a45ff)](https://github.com/DetachHead/basedpyright)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://pre-commit.com/)
[![pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)](https://docs.pytest.org/)
[![Dependabot](https://img.shields.io/badge/dependabot-enabled-025E8C?logo=dependabot)](https://github.com/aviadr1/figmaclaw/security/dependabot)

Mirror Figma pages into local markdown so developers and agents can work against files, not slow UI/API loops.

## Why Developers Care

- `figma/**` becomes a grep-able design memory in your repo.
- Figma structure and product code can evolve in the same PR.
- Design context is versioned in git history, not trapped in screenshots.
- Sync is incremental at file/page/frame levels to keep CI cost low.

## Why This Is Great For Claude/Codex

Agent systems are strongest with local files. `figmaclaw` gives them structured frontmatter and prose context in markdown, so they can:

- search design context instantly with `rg`
- reason over history via git instead of repeated remote calls
- update code and design documentation in one branch

## Why It's Great For Design-System Evolution

Migrating a Figma file from one design system to another (or auditing one for token coverage, or verifying a DS rollout actually landed) is the kind of work where a one-shot agent pass *looks* right but produces silent corruption — invisible inheritance leaks, frozen literals on unbind, components that detach from their masters.

The `audit-page` / `audit-pipeline` / `apply-tokens` command family turns those passes into deterministic pipelines: every change is encoded as a manifest, linted against accumulated invariants (the `FCLAW` rule namespace), applied in atomic batches, and verified after each batch via REST. Round 1 of a real-world migration produced 365 silently-incorrect bindings; the rules and verifiers in this command family exist so round N doesn't.

The migration commands share a common `--dry-run` / `--emit-only` / `--execute` discipline and a use_figma batch-emission protocol (`<prefix>-NNNN.{json,use_figma.js}` + `manifest.json`). All emitted JS templates are F17/F22/F30 compliant — never `.detach()`, never `throw` on aggregate stats — so a single bad row never rolls back its successful sibling writes.

| Command | Purpose |
|---|---|
| `audit-page emit-clone-script` | Clone a source page into an audit page (warns if the source looks inactive — archive, playground, prior audit clone, etc.). |
| `audit-page swap` | Apply component-instance swaps from a typed manifest. Emits per-row try/catch JS, persists the SPD idMap so subsequent `apply-tokens` runs target NEW instances. |
| `audit-pipeline lint` | Validate the `component_migration_map.v3.json` (nested + flat shapes); with `--variants <taxonomy.json>` enforces variant-axis names, values, and OLD-axis coverage. |
| `apply-tokens` | Apply variable-binding fixes; legacy compact-row + versioned manifest accepted. Refusals list unrecognised + missing canonical fields, plus a `did_you_mean_token_name` hint when a `<library>:` prefix is detected. **F41:** falls back to `importVariableByKeyAsync(catalog_key)` so published DS variables not yet in a file's local cache still bind. **F48:** identical-cause runtime errors (font, permission, rate-limit, …) trigger one F36 stderr block + exit 78 instead of N walls of identical lines. See [docs/migration-pipeline.md](docs/migration-pipeline.md#apply-tokens-f48-abort-surface--reportoperator_action). |

See [docs/migration-pipeline.md](docs/migration-pipeline.md) for the full pipeline.

## Setup Is Two Separate Steps

`figmaclaw` setup has two distinct scopes:

1. Install the `figmaclaw` CLI on your machine/CI runner.
2. Initialize a target repository that will store your Figma mirror (`figma/**`).

Installing the CLI does not create mirror workflows or tracked-file state. `figmaclaw init` does.

## Step 1: Install The CLI

### Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/aviadr1/figmaclaw/main/install.sh | sh
```

### Manual install

```bash
uv tool install git+https://github.com/aviadr1/figmaclaw
```

### Upgrade

```bash
figmaclaw self update
```

Equivalent manual command:

```bash
uv tool install --force --reinstall --upgrade git+https://github.com/aviadr1/figmaclaw@main
```

## Step 2: Initialize A Consumer Repo

Choose or create the repository that should hold your Figma markdown mirror:

```bash
mkdir -p /path/to/design-memory
cd /path/to/design-memory
git init
```

Then initialize:

```bash
figmaclaw init --repo-dir /path/to/design-memory
```

What `init` sets up in that target repo:

- `figma/**` markdown mirror files
- `.figma-sync/**` tracking/sync metadata
- `.github/workflows/figmaclaw-*.yaml` managed caller stubs

## Quick Start (After Init)

```bash
# 1) API key
export FIGMA_API_KEY=figd_...

# 2) Track at least one file
figmaclaw track <file-key>

# 3) Pull latest Figma state
figmaclaw pull

# 4) Verify install + managed workflows
figmaclaw doctor
figmaclaw workflows doctor
```

## How It Works

```text
Designer saves in Figma
  -> webhook / hourly cron
  -> figmaclaw pull (fetches Figma API, updates frontmatter)
  -> figmaclaw claude-run (optional enrichment)
      -> inspect: what needs descriptions?
      -> screenshots --stale: download only changed frames
      -> write-body: apply prose updates
      -> mark-enriched: record completion
  -> git commit + push
```

## Common Commands

- `figmaclaw track`: register a Figma file for syncing.
- `figmaclaw pull`: bulk sync all tracked files.
- `figmaclaw sync`: re-sync one markdown page file.
- `figmaclaw inspect`: report structure + enrichment state.
- `figmaclaw inspect-instance`: diff one INSTANCE against its master.
- `figmaclaw screenshots`: download frame screenshots.
- `figmaclaw write-body`: write prose body while preserving frontmatter.
- `figmaclaw mark-enriched`: record enrichment completion state.
- `figmaclaw workflows doctor|upgrade`: detect/repair workflow template drift.

## Documentation

- Installation and CI setup: [docs/INSTALL.md](docs/INSTALL.md)
- Token auth and rotation: [docs/token-auth-and-rotation.md](docs/token-auth-and-rotation.md)
- Markdown schema: [docs/figmaclaw-md-format.md](docs/figmaclaw-md-format.md)
- Migration / lint / apply-tokens pipeline: [docs/migration-pipeline.md](docs/migration-pipeline.md)
- Body preservation design: [docs/body-preservation-design.md](docs/body-preservation-design.md)
- Body preservation invariants: [docs/body-preservation-invariants.md](docs/body-preservation-invariants.md)
- Failure postmortem and lessons: [docs/failure-postmortem-2026-04-03.md](docs/failure-postmortem-2026-04-03.md)

## Relationship To issueclaw

`figmaclaw` follows the same core architecture as [issueclaw](https://github.com/aviadr1/issueclaw) (webhook -> fetch -> render -> commit) but targets Figma. Consumer repos can use both (`figma/` + `linear/`) together.

## Development

```bash
git clone https://github.com/aviadr1/figmaclaw
cd figmaclaw
./install.sh

uv run ruff format --check .
uv run ruff check .
uv run --group dev python -m basedpyright
uv run python -m pytest -q --cov=figmaclaw --cov-report=term-missing --cov-fail-under=70
```

## Troubleshooting

- `MCP initialize has no Mcp-Session-Id`: supported; `FigmaMcpClient` handles sessionful and stateless MCP responses.
- `No Figma token found`: set `FIGMA_MCP_TOKEN` or authenticate Figma in Claude Code (`~/.claude/.credentials.json`).
- `pre-commit` missing locally: run `uv run --with pre-commit python -m pre_commit install`.
