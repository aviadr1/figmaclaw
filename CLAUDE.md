# figmaclaw

## Output format — design contract

**Full format spec:** [`docs/figmaclaw-md-format.md`](docs/figmaclaw-md-format.md) — authoritative reference for frontmatter schema, body structure, command responsibilities, and known limitations.

**Design contract — this is law, never violate it:**
- **Frontmatter** = machine-readable source of truth. CI/CD reads and writes this. Use it to know WHAT needs updating (which frames exist, which are new, which changed).
- **Body** = human/LLM prose + Mermaid charts. Written by humans and LLMs **only**. Never parsed by code. Never mechanically rewritten by code.
- To update a page: read the existing body, fetch new Figma data using the frontmatter, pass both to the LLM, LLM rewrites the body preserving page summary and section intros.
- `set-frames` writes frontmatter only. After `set-frames`, run the skill (LLM) to update the body.
- NEVER add `parse_page_summary()`, `parse_section_intros()`, or any code that reads prose from the body.

**Frontmatter fields:**

| Field | Type | Purpose |
|---|---|---|
| `file_key` | string | Figma file key (for API calls) |
| `page_node_id` | string | Figma CANVAS node ID |
| `frames` | single-line flow-style `{node_id: desc}` | Authoritative frame descriptions |
| `flows` | single-line flow-style `[[src, dst], ...]` | Prototype navigation edges |
| `section_node_id` | string | Component library files only |

**Format rules:**
- `frames` and `flows` must be single-line YAML flow style — never block-indented
- `yaml.dump` must use `width=2**20` to prevent PyYAML from wrapping long values
- Never parse body table cells or prose for node IDs, descriptions, or flows — read frontmatter
- To write descriptions: `figmaclaw set-frames` (frontmatter only) or `figmaclaw enrich` (full body rebuild)
- `set-frames` does NOT update the body — run `figmaclaw enrich` after to regenerate prose

**Update paths:**

| Path | Writes | When to use |
|---|---|---|
| `set-frames` | Frontmatter only (`frames` dict) | Surgical: write frame descriptions from AI/screenshots |
| skill (`figma-enrich-page`) | Full body via LLM | Always use this to update prose — it reads existing body + new Figma data and rewrites via LLM |
| `pull` | Frontmatter + skeleton body | Initial sync only (new pages with no existing prose) |

**Known issue:** `pull` and `enrich` (CLI, no LLM) currently overwrite the body without calling the LLM, destroying `page_summary` and section intros. Fix: CI hooks must call the skill (LLM path) for all page updates, not just `pull`/`enrich`. Tracked in ENG.

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
