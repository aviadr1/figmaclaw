# figmaclaw

## Ecosystem ownership

figmaclaw and issueclaw are **general-purpose open-source tools** — they work for any company, not just Gigaverse. Consumer repos (e.g. `gigaverse_app/linear-git`) are company-specific knowledge repositories.

| Repo | Role | Owns |
|------|------|------|
| **figmaclaw** (this repo) | General-purpose Figma→Git sync | CLI, sync/enrichment logic, reusable CI workflows, enrichment skills, prompt templates, `claude_run.py` launcher |
| **issueclaw** | General-purpose Linear→Git sync | CLI, sync/push/webhook logic, reusable CI workflows |
| **Consumer repos** (e.g. linear-git) | Company-specific knowledge repo | Accumulated markdown data. Consumes figmaclaw + issueclaw via pip + reusable workflows |

**The rule:** All reusable algorithms, scripts, CI workflows, and LLM prompts belong in the tooling repos (figmaclaw / issueclaw). Consumer repos are pure data — they call reusable workflows with repo-specific config (secrets, schedules, team IDs) but never define tooling logic locally. If you find tooling code in a consumer repo, port it upstream to the appropriate tooling repo.

**What belongs here (figmaclaw):**
- `figmaclaw` CLI package — all commands (sync, pull, write-body, mark-enriched, screenshots, etc.)
- `.github/workflows/sync.yml`, `webhook.yml`, `claude-run.yml` — reusable workflows called by consumer repos
- `figmaclaw/skills/` — LLM skills (figma-enrich-page)
- `figmaclaw/templates/` — workflow templates scaffolded to consumer repos by `figmaclaw init`
- `scripts/claude_run.py` — generic Claude Code launcher for CI enrichment
- `prompts/` — prompt templates for enrichment
- `stream-formatter.py` — CI log formatter for Claude stream-json output
- All tests for the above

**What does NOT belong here:**
- Figma page data (`.md` files with frontmatter) — those live in consumer repos
- Repo-specific CI config (secrets, cron schedules, team IDs) — those live in consumer repo workflow callers

## Output format — design contract

**Full format spec:** [`docs/figmaclaw-md-format.md`](docs/figmaclaw-md-format.md)
**Body preservation invariants:** [`docs/body-preservation-invariants.md`](docs/body-preservation-invariants.md)
**Frontmatter v2 design:** [`docs/frontmatter-v2-plan.md`](docs/frontmatter-v2-plan.md)

**Design contract — this is law, never violate it:**
- **Frontmatter** = machine-readable metadata about the page. Tracks what exists, what changed, and whether the body is stale. Use it to make enrichment decisions cheaply (no API calls).
- **Body** = human/LLM prose + Mermaid charts. Written by humans and LLMs **only**. Never parsed by code. Never mechanically rewritten by code.
- **Manifest** = sync engine cache. Recomputable, lossy. If deleted, sync re-fetches everything.
- NEVER add `parse_page_summary()`, `parse_section_intros()`, or any code that reads prose from the body.

**Frontmatter fields:**

| Field | Updated by | Purpose |
|---|---|---|
| `file_key` | `sync` | Figma file identity |
| `page_node_id` | `sync` | Figma page identity |
| `frames` | `sync` | List of frame node IDs — what screens exist |
| `flows` | `sync`, `set-flows` | Prototype navigation edges |
| `enriched_hash` | `mark-enriched` | Page hash at last enrichment (null = never) |
| `enriched_at` | `mark-enriched` | Timestamp of last enrichment |
| `enriched_frame_hashes` | `mark-enriched` | Per-frame content hashes at last enrichment |

**Commands:**

| Command | What it does | Touches body? |
|---|---|---|
| `sync` | Fetch structure from Figma, update frontmatter + manifest | NEVER |
| `pull` | Bulk sync all tracked files | NEVER |
| `write-body` | LLM writes page prose | YES — preserves frontmatter |
| `mark-enriched` | Snapshot current hashes as enriched | NO |
| `mark-stale` | Force re-enrichment | NO |
| `inspect` | Check page structure + enrichment state | NO (read-only) |
| `set-flows` | LLM writes inferred flows | NO (frontmatter only) |
| `screenshots` | Download frame PNGs | NO |

**Enrichment flow:**
```
inspect → screenshots --stale → LLM writes body → write-body → mark-enriched
```

**Body preservation invariants (BP-1 through BP-6):** No CLI command can destroy body content. Tested in `tests/test_body_preservation.py`.

## Development

```bash
# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=figmaclaw --cov-report=term-missing

# Install dev version
./install.sh
```

## Code conventions

- **Use pydantic, not dataclass**, for structured values (decisions, results, model
  rows, anything with named fields). Use `pydantic.BaseModel` with
  `model_config = pydantic.ConfigDict(frozen=True)` when immutability matters.
  Rationale: validation, JSON-serialization, and consistency with existing
  models (`ClaudeResult`, figma models) all come for free. `@dataclass` should
  only appear if there is a concrete reason pydantic cannot meet (there almost
  never is).
- **Pure functions for decisions.** Budget decisions, verdict computation, and
  other branching logic should be pure functions with explicit inputs. No
  clock reads, no environment variables, no I/O inside the decision function.
  Callers pass the observable state in; the decision function maps it to a
  frozen pydantic model. This is what makes the logic testable with golden-log
  assertions (see `figmaclaw/budget.py`, `figmaclaw/verdict.py`).

## Testing conventions

- Write invariant-based tests (what should always be true), not bug-affirming tests
- Use `patch.object` — never `patch` with string paths
- Never mock Pydantic models — create real instances
- All imports at file top, never inside functions
- 100% coverage for new code
- Exit code 0 = success, exit code 2 = error. Never use exit 1 for business logic.
