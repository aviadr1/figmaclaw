# figmaclaw — Practices & Notes

**Purpose:** Turn Figma pages into AI-readable semantic markdown maps stored in git.
**Pattern:** Separate tool (own repo), same architecture as issueclaw — webhook → fetch from API → render → commit. Read-only: never write back to Figma.

---

## Current State

figmaclaw is a **pure data-fetch tool** with zero LLM dependency. LLM enrichment runs via Claude Code CLI in consumer repo CI pipelines.

**Core pipeline:** `sync`/`pull` → fetch Figma API → update frontmatter + manifest → scaffold body for new pages only → auto-commit.

**Enrichment pipeline (CI):** `inspect` → `screenshots --stale` → LLM writes descriptions → `write-body` → `mark-enriched`.

**Key design decisions:** See [`docs/frontmatter-v2-plan.md`](docs/frontmatter-v2-plan.md) for the full rationale.

---

## Engineering Standards (non-negotiable)

### Pydantic for Everything — No Dataclasses
Every structured object is a `pydantic.BaseModel`. Zero `@dataclass`. Zero naked dicts passed between functions.
This means: API response models, manifest models, frontmatter/metadata models, diff result models, parsed page models — all Pydantic.
If you find yourself writing `@dataclass`, stop and write `class Foo(BaseModel)` instead.
Benefits: validation on construction, `model_dump_json()` for free, type-safe field access, easy testing.

### Frontmatter Policy — Structured Data Belongs in YAML Frontmatter
All structured information needed by machines in a `.md` file goes in the YAML frontmatter block.
The frontmatter schema is YAML; every frontmatter shape must have a corresponding Pydantic `BaseModel` class.
The markdown body (tables, headings, prose) is for human and AI reading only — never parse it programmatically.
See `figma_frontmatter.py` → `FigmaPageFrontmatter` as the reference implementation.

### Test-Driven Development
- Write the test first. Always. Even for a 5-line helper.
- Smoke test the API/integration layer before writing any implementation code.
- Tests describe *invariants* ("what must always be true"), not bug reproductions.
- Every new module needs tests that prove it actually works end-to-end, not just that it doesn't crash.
- Run tests before every commit. No exceptions.

### Type Checking — basedpyright (strict)
- `from __future__ import annotations` at top of every file.
- Full type annotations on every function signature and class field.
- No `Any` unless genuinely unavoidable and commented why.
- `pyproject.toml` must have `[tool.basedpyright]` with `typeCheckingMode = "strict"`.
- CI must run `basedpyright` and fail on errors.

### Linting & Formatting — ruff
- Ruff for both formatting (`ruff format`) and linting (`ruff check`).
- Target Python 3.12+.
- pyproject.toml: `[tool.ruff]` with `line-length = 100`, `[tool.ruff.lint]` with at minimum `select = ["E", "F", "I", "UP", "B", "SIM"]`.
- CI must run `ruff check` and `ruff format --check`.

### Pre-commit Hooks
Required hooks in `.pre-commit-config.yaml`:
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.0
    hooks:
      - id: ruff
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-toml
```
Run `pre-commit install` in `install.sh`. CI runs `pre-commit run --all-files`.

### Commit Discipline
- Commit after every working increment — never let more than one module accumulate uncommitted.
- Commit message format: `feat:`, `fix:`, `test:`, `chore:`, `docs:`.
- Test must pass before commit. Pre-commit hook catches formatting; you still run pytest manually.
- Small commits beat large ones. If a commit message needs "and" in it, split it.

---

## Architecture Notes

### Incremental Pull — Key Feature
Three levels of short-circuiting:
1. **File level:** `version` + `lastModified` from depth=1 fetch. If both match manifest → skip the entire file. No page fetches.
2. **Page level:** structural hash over `(node_id, name, type, parent_id)` for all FRAME/SECTION nodes. If hash matches manifest → skip page.
3. **Frame level:** per-frame content hashes (depth-1 children). Only re-enrich frames whose hash changed.

The goal: a nightly pull of a 10-page file with no structural changes makes **zero page fetches** (only 1 cheap file-meta call per file).

### Tree Traversal Rules (confirmed from live Figma API)
- Top-level children of a page (CANVAS) are `SECTION` nodes — each maps to a `FigmaSection`.
- Children of a SECTION that are `FRAME` → `FigmaFrame`.
- Filter out `CONNECTOR` nodes (appear in section children, are visual connectors not screens).
- If a page has top-level `FRAME` nodes (no sections), group them into a `(Ungrouped)` section.
- Prototype reactions are empty on most pages — don't count on them for flow edges.

### Figma API — Key Facts
- Auth header: `X-Figma-Token: {api_key}` — NOT `Authorization: Bearer`.
- `GET /v1/files/{key}?depth=1` → returns name, version, lastModified, all pages. Fast.
- `GET /v1/files/{key}/nodes?ids={page_id}` → returns full tree for one page.
- `GET /v2/webhooks` → lists webhooks (team-scoped).
- `POST /v2/webhooks` → registers webhook at team level.

### COMMIT_MSG Protocol (same as issueclaw)
Commands print `COMMIT_MSG:{message}` on stdout. GitHub Actions extracts it.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `FIGMA_API_KEY` | Yes | Figma personal access token (`figd_...`). Use `X-Figma-Token` header. |
| `FIGMA_WEBHOOK_SECRET` | Webhook only | Passcode registered with Figma webhook, validated in CF Worker |
| `CLAUDE_CODE_OAUTH_TOKEN` | CI enrichment | Claude Code CLI auth for enrichment workflows |

---

## Key Files

- `.figma-sync/manifest.json` — sync engine cache (committed, schema versioned)
- `figma/` — output directory, one `.md` per Figma page
- `docs/frontmatter-v2-plan.md` — authoritative design document
- `docs/body-preservation-invariants.md` — tested invariants for body safety

## Refactoring Backlog

- `pull_logic.py` is getting long — consider extracting `_process_page()` as a separate testable function
- `commands/pull.py` and `commands/apply_webhook.py` both call `pull_file` with the same state setup pattern — extract a shared helper
- Test mocks for `FigmaClient.get_page` are inconsistent — audit all mocks
