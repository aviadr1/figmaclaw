# Body preservation invariants

The body of a figmaclaw `.md` file is LLM-authored prose: page summaries,
section intros, filled description tables, and Mermaid flowcharts. Producing
it costs Figma screenshots + LLM inference + human review. Losing it silently
is unacceptable.

These invariants are **law**. Every code path that touches `.md` files must
obey them. Tests in `tests/test_body_preservation.py` and `tests/test_replace_body.py` prove each one.

**Related docs:**
- [`figmaclaw-md-format.md`](figmaclaw-md-format.md) — authoritative format spec (frontmatter schema, body structure, command table)
- [`body-preservation-design.md`](body-preservation-design.md) — original design doc (historical context for why these invariants exist)

## Terminology

| Term | Meaning |
|---|---|
| **Frontmatter** | YAML between `---` delimiters. Machine-readable source of truth. Code reads and writes this freely. |
| **Body** | Everything below the closing `---`. LLM/human prose only. Code NEVER writes, parses, or regenerates it. |
| **Scaffold** | Initial skeleton body for a NEW page — contains `<!-- LLM: ... -->` placeholders. Written once when the file doesn't exist. |

## BP: Body preservation invariants

These prove that no code path can destroy LLM-authored body content.

| ID | Invariant | Enforced by |
|---|---|---|
| **BP-1** | `sync` on an existing file preserves the body byte-for-byte | `test_bp1_sync_preserves_body_byte_for_byte` |
| **BP-2** | `pull_file` on an existing file preserves the body byte-for-byte | `test_bp2_pull_preserves_body_byte_for_byte` |
| **BP-3** | `set-frames` on an existing file preserves the body byte-for-byte | `test_bp3_set_frames_preserves_body_byte_for_byte` |
| **BP-4** | `update_page_frontmatter()` preserves the body byte-for-byte | `test_bp4_update_page_frontmatter_preserves_body` |
| **BP-5** | `scaffold_page()` is never called on existing files by `sync` or `pull` | `test_bp5_sync_does_not_call_scaffold_on_existing_file`, `test_bp5_pull_does_not_call_scaffold_on_existing_file` |
| **BP-6** | `replace-body` preserves frontmatter byte-for-byte | `test_bp6_replace_body_preserves_frontmatter_byte_for_byte` |

**Bonus invariants (stress tests):**
- Body survives 5 consecutive `sync` operations without degradation
- Body survives interleaved `sync` + `set-frames` cycles

## SC: Scaffold invariants

These prove that new files get proper LLM placeholders.

| ID | Invariant | Enforced by |
|---|---|---|
| **SC-1** | `sync` on a non-existent file writes a scaffold with LLM placeholders | `test_sc1_sync_writes_scaffold_for_new_file` |
| **SC-2** | `pull_file` on a non-existent file writes a scaffold with LLM placeholders | `test_sc2_pull_writes_scaffold_for_new_file` |
| **SC-3** | Scaffold contains `<!-- LLM: ... -->` placeholders for page summary, section intros, and Mermaid | `test_sc3_scaffold_contains_all_llm_placeholders` |

## FM: Frontmatter correctness invariants

These prove that frontmatter is correct after body-preserving operations.

| ID | Invariant | Enforced by |
|---|---|---|
| **FM-1** | Existing frame descriptions survive `sync` | `test_fm1_descriptions_survive_sync` |
| **FM-2** | Existing flows survive `sync` | `test_fm2_flows_survive_sync` |
| **FM-3** | New frames from Figma appear in frontmatter after `sync` | `test_fm3_new_frames_appear_after_sync` |
| **FM-4** | Frontmatter is valid `FigmaPageFrontmatter` after `sync` | `test_fm4_frontmatter_valid_after_sync` |

## CL: CLI flag invariants

These prove that informational flags never modify the file.

| ID | Invariant | Enforced by |
|---|---|---|
| **CL-1** | `--scaffold` prints to stdout without modifying the file | `test_cl1_scaffold_flag_does_not_modify_file` |
| **CL-2** | `--show-body` prints to stdout without modifying the file | `test_cl2_show_body_flag_does_not_modify_file` |

## How the code enforces these invariants

### Existing files: frontmatter-only update

```
sync/pull → file exists?
  YES → update_page_frontmatter()
         1. frontmatter.loads(md) → separates frontmatter from body
         2. build_page_frontmatter(page) → generates new YAML block
         3. Writes: {new frontmatter}\n\n{original body}
  NO  → write_new_page()
         1. scaffold_page(page, entry) → generates scaffold with LLM placeholders
         2. Writes to disk
```

### Key functions

| Function | What it does | When to call |
|---|---|---|
| `scaffold_page()` | Generates skeleton markdown with `<!-- LLM: ... -->` placeholders | New files only |
| `write_new_page()` | Calls `scaffold_page()` and writes to disk | New files only |
| `update_page_frontmatter()` | Replaces YAML frontmatter, preserves body byte-for-byte | Existing files only |
| `build_page_frontmatter()` | Builds frontmatter string from `FigmaPage` model | Used by `update_page_frontmatter()` |
| `write_body()` | Writes body only, preserves frontmatter | LLM body updates only |

### The LLM update path

The LLM must **always** see the existing body. It receives three inputs:

1. **Existing body** — verbatim, via `split_frontmatter()` or `--show-body`
2. **Updated frontmatter** — from `sync`, shows current frame/flow structure
3. **Scaffold (optional)** — via `--scaffold`, structural hint with placeholders

The LLM rewrites the body preserving existing prose. The `write-body` command
writes the result back without touching frontmatter. Then `mark-enriched`
snapshots the current hashes so the system knows the body is up to date.

See [`frontmatter-v2-plan.md`](frontmatter-v2-plan.md) for the full enrichment flow and design rationale.

## What NOT to do

- NEVER call `scaffold_page()` on an existing file
- NEVER call `write_new_page()` on an existing file
- NEVER parse body prose in Python code (no `parse_page_summary()`, no regex on body tables)
- NEVER add code that reads structured data from the body — frontmatter is the source of truth
- NEVER regenerate the body from code — only the LLM rewrites it
