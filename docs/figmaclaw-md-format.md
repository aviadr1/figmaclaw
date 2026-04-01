# figmaclaw `.md` File Format Specification

Every Figma page is rendered as one `.md` file under `figma/{file-slug}/pages/` (screen pages) or `figma/{file-slug}/components/` (component library sections). This document is the authoritative specification for that format.

---

## Design contract

| Layer | Who reads it | Who writes it | Authority |
|---|---|---|---|
| YAML frontmatter | CI/CD, figmaclaw CLI, agents | figmaclaw CLI only | **Machine-readable source of truth** |
| Markdown body | Humans, AI agents, LLMs | LLMs via `figma-enrich-page` skill | Human/LLM-written prose — never parsed by code |

### The law: body is not parsed, ever

The body is written by humans and LLMs. It contains:
- **Page summary** — LLM-written paragraph describing what this Figma page covers
- **Section intros** — LLM-written one-sentence intro per section
- **Frame tables** — LLM-written per-frame descriptions in table form
- **Mermaid flowchart** — generated from `flows` frontmatter

**No Python code, no CLI command, no agent tool should ever parse prose from the body.** No `parse_page_summary()`. No `parse_section_intros()`. No extracting text between headings.

### The correct update path

To update a page that already has LLM-generated prose:
1. Read the **existing `.md` body** — pass it verbatim to the LLM
2. Use the **frontmatter** to know what changed: which frames are new, which have no description, what the current flows are
3. Fetch **screenshots** for frames that need updating
4. LLM receives: existing body + new frame descriptions/screenshots → rewrites the body, preserving page summary and section intros where still accurate
5. Write the result back to disk

This is what the `figma-enrich-page` skill implements.

**The body is never generated from scratch on re-sync.** Only new pages (no existing body) get a skeleton body from `scaffold_page()`. Existing pages get frontmatter-only updates via `update_page_frontmatter()`. See [`docs/body-preservation-invariants.md`](body-preservation-invariants.md) for the full invariant list.

### What frontmatter is for (vs body)

Use frontmatter to answer: "what needs updating?" — which frames exist, which are missing descriptions, what the prototype flows are. Do not parse the body for any of this. Frontmatter is the index; body is the prose.

---

## Frontmatter schema

All frontmatter is YAML between `---` markers at the top of the file.

### Screen page (`.../pages/*.md`)

```yaml
---
file_key: 7az6PPiHUQumhxtV935xuD
page_node_id: 4554:17865
frames: {4713:6926: 'Prepare screen showing camera preview and Go Live button.', 4713:7191: 'Countdown screen showing LIVE IN 3.'}
flows: [["4713:6926", "4713:7191"], ["4713:7191", "4713:7445"]]
---
```

| Field | Type | Required | Description |
|---|---|---|---|
| `file_key` | string | yes | Figma file key — used for all API calls |
| `page_node_id` | string | yes | Figma CANVAS node ID for this page |
| `frames` | flow-style mapping | no | `{node_id: description}` — authoritative frame descriptions |
| `flows` | flow-style sequence | no | `[[src_id, dst_id], ...]` — prototype navigation edges |

### Component library section (`.../components/*.md`)

Same as above plus:

| Field | Type | Required | Description |
|---|---|---|---|
| `section_node_id` | string | yes | Figma node ID of the section within the page |

### Format invariants — frontmatter

- `frames` **must** be a single-line YAML flow mapping: `frames: {key: value, ...}` — NOT block-indented YAML
- `flows` **must** be a single-line YAML flow sequence: `flows: [["a", "b"], ...]`
- PyYAML requires `width=2**20` in `yaml.dump` to prevent wrapping long values — without it, descriptions with apostrophes or colons break into ugly multi-line flow style
- `page_hash` is **NOT** stored in `.md` files — it lives only in `.figma-sync/manifest.json`
- Empty descriptions are **not written** to frontmatter — a missing key means no description yet (equivalent to `(no description yet)` in the body)
- Node IDs containing `:` (e.g. `4713:6926`) are valid YAML map keys — no quoting needed

### Editing frontmatter

- **Do:** Use `figmaclaw set-frames` to merge descriptions into `frames:`
- **Do:** Edit `frames:` directly in the file if needed
- **Don't:** Edit other frontmatter fields manually — they are managed by figmaclaw
- **Don't:** Add `page_hash` or any legacy `figmaclaw:` nested block

---

## Body structure

The body is written by the LLM (via the `figma-enrich-page` skill) and is prose built from frame descriptions, screenshots, and Figma structure. It is **not authoritative** for any machine-readable data — frontmatter is. New pages get a scaffold with `<!-- LLM: ... -->` placeholders; existing pages have their body preserved across `sync` and `pull` operations.

### Screen page body

```markdown
# {file_name} / {page_name}

[Open in Figma]({figma_deep_link})

{optional page summary paragraph — one paragraph, plain prose, LLM-generated}

## {section_name} (`{section_node_id}`)

{optional section intro sentence — one sentence, LLM-generated}

| Screen | Node ID | Description |
|--------|---------|-------------|
| {frame_name} | `{node_id}` | {description or PLACEHOLDER} |

## Screen Flow

```mermaid
flowchart LR
    n4713_6926["Prepare screen"] --> n4713_7191["Countdown 3"]
```
```

### Component library body

```markdown
# {file_name} / {page_name} / {section_name}

[Open in Figma]({section_deep_link})

## Variants (`{section_node_id}`)

| Variant | Node ID | Description |
|---------|---------|-------------|
| {variant_name} | `{node_id}` | {description or PLACEHOLDER} |
```

### Body invariants

- H1 format: `# {file_name} / {page_name}` (screen) or `# {file_name} / {page_name} / {section_name}` (component)
- Section headers: `## {name} (\`{node_id}\`)` — the node ID in backticks inside parens is required for `figma_md_parse.parse_sections()` to parse it
- Table columns: `| Screen | Node ID | Description |` for screen pages; `| Variant | Node ID | Description |` for component sections
- Placeholder for missing descriptions: `(no description yet)` — defined in `figmaclaw/figma_render.py::PLACEHOLDER`
- `## Screen flows` section: **always present**; contains a Mermaid `flowchart LR` block built from `flows:` frontmatter (authoritative Figma reactions) and design inspection via screenshots
- Node IDs in Mermaid: prefixed with `n` and `:` replaced with `_` (e.g. `4713:6926` → `n4713_6926`)
- No "Quick Reference" table — that section was removed

### What the body is NOT

- The body description column in the table is a **display copy** of `frames[node_id]` from frontmatter. It is not the source of truth.
- Agents and tooling **must never parse the description column** to get frame descriptions — read `frames` from frontmatter.
- Section intros and page summary are **only in the body** — they are not stored in frontmatter. See known limitations below.

---

## Command responsibilities

| Command | Reads | Writes | Body touched? |
|---|---|---|---|
| `figmaclaw sync` | Figma API + existing frontmatter | Frontmatter only (existing files) / scaffold (new files) | **Never** for existing files. New files get scaffold with LLM placeholders. |
| `figmaclaw pull` | Figma API + existing frontmatter | Frontmatter only (existing files) / scaffold (new files) | **Never** for existing files. Same as `sync` but bulk. |
| `figmaclaw set-frames` | Existing frontmatter | `frames:` in frontmatter only | **Never** |
| `figmaclaw replace-body` | Existing frontmatter + new body from stdin/flag | Body only | **Yes** — replaces body, preserves frontmatter byte-for-byte |
| `figmaclaw page-tree` | Frontmatter + body structure | Nothing | Read-only |
| `figmaclaw screenshots` | Frontmatter + body structure | PNG files in `.figma-cache/` | No |

### Why code never touches the body

The body is LLM-authored prose — page summary, section intros, Mermaid charts, filled description tables. Producing it costs Figma screenshots + LLM inference + human review. No CLI command regenerates it.

- `set-frames` writes frontmatter only. The LLM reads descriptions from frontmatter.
- `sync` and `pull` update frontmatter only for existing files.
- `replace-body` is the LLM's tool for writing the body back after rewriting it.
- The `figma-enrich-page` skill orchestrates the full flow: screenshots → describe → set-frames → LLM rewrites body → replace-body.

See [`docs/body-preservation-invariants.md`](body-preservation-invariants.md) for the tested invariants.

---

## Known limitations (bugs)

### set-frames reports success even when frontmatter parse fails

**Status:** Bug — tracked as ENG-XXXX

If the `.md` file has malformed YAML frontmatter, `parse_frontmatter()` returns `None` silently and `set-frames` writes the unchanged file back, printing "wrote N description(s)" even though nothing was merged.

**Workaround:** Validate the file with `figmaclaw page-tree` before and after `set-frames`.

---

## Parsing the format

### Reading frame descriptions

```python
from figmaclaw.figma_parse import parse_frontmatter

fm = parse_frontmatter(md_text)
if fm is None:
    # No figmaclaw frontmatter or malformed YAML
    ...
descriptions = fm.frames  # dict[str, str]: {node_id: description}
```

### Reading page structure (node IDs, section names)

```python
from figmaclaw.figma_md_parse import parse_sections

sections = parse_sections(md_text)
for section in sections:
    print(section.name, section.node_id)
    for frame in section.frames:
        print(frame.name, frame.node_id)
        # frame.description does NOT exist — read from fm.frames instead
```

### Writing descriptions

```bash
# Always use stdin heredoc — never --frames for multi-value JSON with apostrophes
figmaclaw set-frames figma/<file>/pages/<page>.md << 'EOF'
{
  "4713:6926": "Prepare screen showing camera preview and a pink Go Live button.",
  "4713:7191": "Countdown screen showing 'LIVE IN 3' in large gradient text."
}
EOF
```

---

## File paths

| Type | Path pattern |
|---|---|
| Screen page | `figma/{file-slug}/pages/{page-slug}-{file_key_suffix}-{page_node_id_suffix}.md` |
| Component section | `figma/{file-slug}/components/{section-slug}-{section_node_id_suffix}.md` |
| Manifest | `.figma-sync/manifest.json` |
| Screenshot cache | `.figma-cache/screenshots/{file_key}/{node_id}.png` (gitignored) |

The slug portion is `slugify(name)` (lowercase, hyphens). The suffix is the node ID with `:` replaced by `-`.
