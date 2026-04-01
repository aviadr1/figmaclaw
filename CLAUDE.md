# figmaclaw

## Output format — design contract

**Full format spec:** [`docs/figmaclaw-md-format.md`](docs/figmaclaw-md-format.md) — authoritative reference for frontmatter schema, body structure, command responsibilities, and known limitations.

**Design contract — this is law, never violate it:**
- **Frontmatter** = machine-readable source of truth. CI/CD reads and writes this. Use it to know WHAT needs updating (which frames exist, which are new, which changed).
- **Body** = human/LLM prose + Mermaid charts. Written by humans and LLMs **only**. Never parsed by code. Never mechanically rewritten by code.
- To update a page: read the existing body, fetch new Figma data using the frontmatter, pass both to the LLM, LLM rewrites the body preserving page summary and section intros.
- `set-frames` writes frontmatter only. After `set-frames`, run the skill (LLM) to update the body.
- `replace-body` writes body only. The LLM uses this to update prose without touching frontmatter.
- NEVER add `parse_page_summary()`, `parse_section_intros()`, or any code that reads prose from the body.

**Body preservation invariants (BP-1 through BP-6):** No CLI command can destroy body content. Tested in `tests/test_body_preservation.py`. Full details: [`docs/body-preservation-invariants.md`](docs/body-preservation-invariants.md).

**Commands** (see [`docs/figmaclaw-md-format.md`](docs/figmaclaw-md-format.md) for full format spec):

| Command | Writes | Body touched? |
|---|---|---|
| `set-frames` | Frontmatter only | Never |
| `replace-body` | Body only | Yes — preserves frontmatter (BP-6) |
| `sync` | Frontmatter only (existing) / scaffold (new) | Never for existing files (BP-1) |
| `pull` | Frontmatter only (existing) / scaffold (new) | Never for existing files (BP-2) |
| skill (`figma-enrich-page`) | Orchestrates full LLM update | LLM rewrites body via `replace-body` |

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
