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
| `component_set_keys` | `pull` | Component sections only. Maps published component-set name → Figma key for `importComponentSetByKeyAsync()`. |
| `raw_frames` | `pull` | Screen pages only. Sparse dict of frames with ≥1 raw (non-INSTANCE) child: `{node_id: {raw: N, ds: [names...]}}`. Absent = fully componentized. |

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

## Write idempotency rule

**Every function that writes a file must be idempotent: skip the write if only a timestamp (`generated_at`, `updated_at`, etc.) would change.**

Rationale: figmaclaw runs in a CI loop. Any unconditional write — even just a timestamp — lands in a git commit, triggers Claude enrichment, and wastes CI budget. The pattern is: load existing content, strip timestamp fields, compare with new content (also stripped of timestamps); write only if data differs. See `_write_token_sidecar` and `save_catalog` for the reference implementation.

**Corollary — bypass flags and `max_pages` budget:** Any flag that bypasses the page-hash check (currently `force`, `schema_stale`) must NOT also consume the `max_pages` budget for pages it processes. If a bypass flag causes every page to be "processed" while also consuming budget, the `while pull` loop will never terminate. Schema-only upgrades are the canonical example: they bypass the hash skip but do not increment `pages_written_this_call`.

## Code conventions

- **Use pydantic, not dataclass**, for structured values (decisions, results, model
  rows, parser/validator outputs, anything with named fields). Use `pydantic.BaseModel` with
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

## Anti-loop engineering policy (figmaclaw#121)

Enrichment runs in a loop (hourly CI). Any bug that causes `claude-run`
to do the same work over and over costs real money and hides real
regressions behind RED-forever CI. We learned this the hard way over
the course of 5+ PRs (#88 #92 #103 #112 #117) that each added
same-run guards but left cross-run failure modes untouched.

When adding or modifying any enrichment / pull / logging code path,
review against these three dimensions in addition to the usual unit /
same-run / schema / body-frontmatter / observability tests:

### 1. Cross-run state invariants

Any guard that prevents retries **within one run** must be paired with
an invariant that prevents retries **across runs**. Test shape:

```
state_0 := fixture
run code against state_0  → produces state_1
run code against state_1  → produces state_2
assert state_2 is cheaper/different from state_1,
       OR state_2 does not re-select work that state_1 already addressed
```

If your PR adds a loop-break (like a NO-PROGRESS guard), also add a
cross-run test showing the selector does not reselect the same file
given the state produced by the previous run. "Same input, same
expensive output, forever" is the bug shape row 9 YELLOW exists to
flag.

### 2. Cross-field frontmatter key-set invariants

The frontmatter has multiple frame-keyed dicts: `enriched_frame_hashes`,
`raw_frames`, `raw_tokens`, `frame_sections`. The invariant
`keys(d) ⊆ frames` must hold for every one of them after every write.
Enforced centrally in `figma_render._build_frontmatter` — the single
chokepoint for frontmatter serialization.

**If you add a new frame-keyed field**:
- Extend `_build_frontmatter` to prune it too
- Add a test in `tests/test_frontmatter_key_set_invariant.py` asserting
  orphan keys are dropped on write
- Do not rely on callers to pre-prune — the chokepoint is the guarantee

### 3. Terminal-state completeness for LLM-dispatched work

Every "pending" state must have a terminal counterpart. If the LLM can
produce an output that says "I cannot resolve this right now" (e.g.
`(no screenshot available)`), that output must be recordable as a
tombstone so we don't re-dispatch the same question on the next run.
Tombstones auto-invalidate when the underlying content hash changes.

**If you introduce a new "soft-done" row marker**: design the tombstone
protocol in the same PR. Do not ship a retryable marker without a
matching terminal state — that guarantees cross-run loops.

### 4. Canonical walker reuse

Body frame-row iteration has exactly one canonical implementation:
`body_validation.iter_body_frame_rows`. Fence-aware, exact rendered
header matching, yields `BodyFrameRow` pydantic models with
`line_index` and `node_id`. Use it for any code that needs to inspect
or mutate canonical body frame tables.

Don't:
- re-implement `is_table_separator` / `parse_frame_row` / fence tracking
  in new code
- copy loops from existing walkers
- import `re` in pull / claude-run / write-body code to match row shapes

Do:
- use `iter_body_frame_rows` for row-by-row work
- use `body_frame_node_ids` (a thin projection over the iterator) when
  you only need the node_id list
- use `section_line_ranges` / `parse_sections` for section-level work

### 5. Log writers — no WARN-and-drop

A log writer that emits `WARN … skipped until file is fixed` but never
fixes the file silently loses data every run forever. Acceptable
resolutions, in order of preference:

1. **Migrate** — if the schema is recognizable (including legacy
   superset/subset cases), migrate in place on the first mismatch.
   This is the happy path.
2. **Auto-archive and reset** — if no migration path applies, rename
   the prior file to `<name>.bak.<UTC-timestamp><ext>` and start a
   fresh schema-v1 log. History is preserved, the writer heals itself,
   and subsequent runs append normally. Required: emit a human-readable
   error line describing the archive event — don't hide the self-heal.
3. **Hard-fail** — only for critical writers where silently resetting
   would corrupt load-bearing state. Log writers used for observability
   do not qualify.

Never accept "warn and forget every run" as a terminal state. The test
shape that pins this: run 1 with bad input triggers the heal, run 2
with no new bad input does NOT re-emit the error.

### Review checklist for enrichment / pull / logging PRs

- [ ] Does this PR add a loop-break? If yes, does it have a cross-run
      test (dimension 1)?
- [ ] Does this PR touch frontmatter fields? If yes, are frame-keyed
      dicts pruned at the chokepoint and covered by key-set tests
      (dimension 2)?
- [ ] Does this PR introduce a new LLM row marker? If yes, is the
      tombstone protocol designed in the same PR (dimension 3)?
- [ ] Does this PR walk the body? If yes, does it use
      `iter_body_frame_rows` / `section_line_ranges` (dimension 4)?
- [ ] Does this PR touch a log writer? If yes, does it auto-heal or
      hard-fail on schema drift (dimension 5)?
