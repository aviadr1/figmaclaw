# figmaclaw

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

## Testing conventions

- Write invariant-based tests (what should always be true), not bug-affirming tests
- Use `patch.object` — never `patch` with string paths
- Never mock Pydantic models — create real instances
- All imports at file top, never inside functions
- 100% coverage for new code
- Exit code 0 = success, exit code 2 = error. Never use exit 1 for business logic.
