# figmaclaw

> **For invariants, design decisions, and the data contract, see [`docs/figmaclaw-canon.md`](docs/figmaclaw-canon.md).** That document is authoritative; this one is a developer-onboarding pointer with brief summaries.

## Ecosystem ownership

figmaclaw and issueclaw are **general-purpose open-source tools** — they work for any company, not just Gigaverse. Consumer repos (e.g. `gigaverse_app/linear-git`) are company-specific knowledge repositories.

| Repo | Role | Owns |
|------|------|------|
| **figmaclaw** (this repo) | General-purpose Figma→Git sync | CLI, sync/enrichment logic, reusable CI workflows, enrichment skills, prompt templates, `claude_run.py` launcher |
| **issueclaw** | General-purpose Linear→Git sync | CLI, sync/push/webhook logic, reusable CI workflows |
| **Consumer repos** (e.g. linear-git) | Company-specific knowledge repo | Accumulated markdown data. Consumes figmaclaw + issueclaw via pip + reusable workflows |

**The rule:** All reusable algorithms, scripts, CI workflows, and LLM prompts belong in the tooling repos (figmaclaw / issueclaw). Consumer repos are pure data — they call reusable workflows with repo-specific config (secrets, schedules, team IDs) but never define tooling logic locally. If you find tooling code in a consumer repo, port it upstream.

**What belongs here (figmaclaw):**
- `figmaclaw` CLI package — all commands (sync, pull, write-body, mark-enriched, screenshots, variables, census, etc.)
- `.github/workflows/sync.yml`, `webhook.yml`, `claude-run.yml`, `census.yml`, `variables.yml` — reusable workflows called by consumer repos
- `figmaclaw/skills/` — LLM skills (figma-enrich-page)
- `figmaclaw/templates/` — workflow templates scaffolded to consumer repos by `figmaclaw init`
- `prompts/` — prompt templates for enrichment
- All tests for the above

**What does NOT belong here:**
- Figma page data (`.md` files with frontmatter) — those live in consumer repos
- Repo-specific CI config (secrets, cron schedules, team IDs) — those live in consumer repo workflow callers
- Customer-specific constants (library hashes, brand names, etc.) — see canon §5 D12

## Data contract

The full three-layer contract is canonized in [`docs/figmaclaw-canon.md` §1](docs/figmaclaw-canon.md#1-three-layer-data-contract). Quick summary:

- **Frontmatter** = machine-readable state. Code reads/writes freely.
- **Body** = human/LLM prose. **Code never writes, regenerates, or parses.** No `parse_page_summary()`. No `parse_section_intros()`. No regex over body tables.
- **Manifest** = sync-engine cache. Recomputable from REST.
- **File-scope registries** (`ds_catalog.json`, `_census.md`) = file-scope cached answers. Recomputable from REST. New layer; see canon §1, D11.

**Detailed file format spec:** [`docs/figmaclaw-md-format.md`](docs/figmaclaw-md-format.md).

## Commands

| Command | What it does | Touches body? |
|---|---|---|
| `sync` | Fetch structure from Figma, update frontmatter + manifest | NEVER |
| `pull` | Bulk sync all tracked files | NEVER |
| `census` | Snapshot published component sets to `_census.md` | NEVER |
| `variables` | Refresh DS variable catalog from `/variables/local` | NEVER |
| `suggest-tokens` | Annotate token sidecars with DS-variable candidates | NEVER (writes sidecar only) |
| `write-body` | LLM writes page prose | YES — preserves frontmatter |
| `mark-enriched` | Snapshot current hashes as enriched | NO |
| `mark-stale` | Force re-enrichment | NO |
| `inspect` | Check page structure + enrichment state | NO (read-only) |
| `set-flows` | Write inferred flows to frontmatter | NO (frontmatter only) |
| `screenshots` | Download frame PNGs | NO |
| `track` / `list` / `init` / `doctor` | Setup and discovery | NO |

**Enrichment flow:** `inspect → screenshots --stale → LLM writes body → write-body → mark-enriched`.

## Invariants — quick index

The full text of every invariant lives in [canon §4](docs/figmaclaw-canon.md#4-invariant-classes). Cite by ID in commit messages and PR review.

| Class | Owns | Canon link |
|---|---|---|
| BP | Body preservation (`.md` body never destroyed by code) | [§4 BP](docs/figmaclaw-canon.md#bp--body-preservation) |
| SC | Scaffold (new files get LLM placeholders) | [§4 SC](docs/figmaclaw-canon.md#sc--scaffold) |
| FM | Frontmatter correctness | [§4 FM](docs/figmaclaw-canon.md#fm--frontmatter-correctness) |
| CL | CLI flag innocence (informational flags don't write) | [§4 CL](docs/figmaclaw-canon.md#cl--cli-flag-innocence) |
| W  | Write idempotency (skip if only timestamp would change) | [§4 W](docs/figmaclaw-canon.md#w--write-idempotency) |
| CR | Cross-run discipline (no same-input-same-expensive-output loops) | [§4 CR](docs/figmaclaw-canon.md#cr--cross-run-discipline) |
| KS | Frame-keyed key-set (`keys(d) ⊆ frames`) | [§4 KS](docs/figmaclaw-canon.md#ks--frame-keyed-key-set) |
| TS | Terminal-state for LLM-dispatched work (every "pending" has a tombstone) | [§4 TS](docs/figmaclaw-canon.md#ts--terminal-state-for-llm-dispatched-work) |
| CW | Canonical walker reuse (one body iterator) | [§4 CW](docs/figmaclaw-canon.md#cw--canonical-walker-reuse) |
| LW | Log-writer auto-heal or hard-fail (no WARN-and-drop) | [§4 LW](docs/figmaclaw-canon.md#lw--log-writer-auto-heal) |
| HE | Heal-at-entry (every reader normalizes on encounter) | [§4 HE](docs/figmaclaw-canon.md#he--heal-at-entry) |
| TC | Token catalog | [§4 TC](docs/figmaclaw-canon.md#tc--token-catalog) |
| TS-S | Token sidecar | [§4 TS-S](docs/figmaclaw-canon.md#ts-s--token-sidecar) |

## Development

```bash
# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=figmaclaw --cov-report=term-missing

# Type check
uv run basedpyright

# Lint
uv run ruff check
uv run ruff format --check

# Install dev version
./install.sh
```

## Code conventions

- **Use pydantic, not dataclass**, for structured values (decisions, results, model rows, parser/validator outputs, anything with named fields). Use `pydantic.BaseModel` with `model_config = pydantic.ConfigDict(frozen=True)` when immutability matters. Rationale: validation, JSON-serialization, and consistency with existing models all come for free. `@dataclass` should only appear if there is a concrete reason pydantic cannot meet (there almost never is).
- **Pure functions for decisions.** Budget decisions, verdict computation, and other branching logic should be pure functions with explicit inputs. No clock reads, no environment variables, no I/O inside the decision function. Callers pass the observable state in; the decision function maps it to a frozen pydantic model. This is what makes the logic testable with golden-log assertions (see `figmaclaw/budget.py`, `figmaclaw/verdict.py`).
- **Library identity is data, never a constant.** No customer-specific hashes (library hash, file_key, brand name) in figmaclaw source. See canon §5 D12.

## Testing conventions

- Write **invariant-based tests** (what should always be true), not bug-affirming tests.
- Use `patch.object` — never `patch` with string paths.
- Never mock pydantic models — create real instances.
- All imports at file top, never inside functions.
- 100% coverage for new code.
- Exit code 0 = success, exit code 2 = error. Never use exit 1 for business logic. (Canon §5 D6.)
- Tests cite invariant IDs by name (e.g. `test_tc1_authoritative_source`).

## Anti-loop policy summary

The full text of dim 1–6 lives in canon §4 (CR, KS, TS, CW, LW, HE). When adding or modifying any enrichment / pull / logging code path, run through the canon's [anti-pattern checklist](docs/figmaclaw-canon.md#8-anti-pattern-checklist-for-pr-review) before opening a PR. The same checklist below, abbreviated:

- [ ] Loop-break? → cross-run test (CR-1).
- [ ] Frame-keyed frontmatter field? → pruned at `_build_frontmatter` chokepoint + key-set test (KS-1).
- [ ] New LLM row marker? → tombstone protocol same PR (TS-1).
- [ ] Body iteration? → use `iter_body_frame_rows` / `section_line_ranges` (CW-1).
- [ ] Log/schema writer? → auto-heal or hard-fail (LW-1, LW-2).
- [ ] New `.md` reader entry? → `normalize_page_file` + register in `_HEALING_ENTRY_POINTS` (HE-1).
- [ ] New writer? → strip timestamps before compare (W-1) + round-trip-assert after write (W-2).
- [ ] Catalog field? → has a writer (TC-3) + correct `source` (TC-2, D14).
- [ ] Catalog refresh? → page-independent (TC-5).
- [ ] `classify_variable_id`? → library identity passed in as data (D12).
- [ ] Sidecar schema change? → migration preserves `fix_variable_id` (LW-2, TS-S-5).
- [ ] New catalog consumer? → CR-2 staleness-check before producing results.
