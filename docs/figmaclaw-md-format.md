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
frames: ['4713:6926', '4713:7191']
flows: [["4713:6926", "4713:7191"], ["4713:7191", "4713:7445"]]
enriched_hash: b39103d8ad45cd38
enriched_at: '2026-04-01T12:00:00Z'
enriched_frame_hashes: {'4713:6926': 'a3f2b7c1', '4713:7191': 'e4d9f8a2'}
---
```

| Field | Type | Required | Description |
|---|---|---|---|
| `file_key` | string | yes | Figma file key — used for all API calls |
| `page_node_id` | string | yes | Figma CANVAS node ID for this page |
| `frames` | flow-style sequence | no | `[node_id, ...]` — list of frame node IDs (descriptions live in body) |
| `flows` | flow-style sequence | no | `[[src_id, dst_id], ...]` — prototype navigation edges |
| `enriched_hash` | string | no | Page hash at time of last enrichment (null = never enriched) |
| `enriched_at` | string | no | ISO timestamp of last enrichment |
| `enriched_frame_hashes` | flow-style mapping | no | Per-frame content hashes at last enrichment |

### Component library section (`.../components/*.md`)

Same as above plus:

| Field | Type | Required | Description |
|---|---|---|---|
| `section_node_id` | string | yes | Figma node ID of the section within the page |

### Format invariants — frontmatter

- `frames` **must** be a single-line YAML flow sequence: `frames: ['id1', 'id2', ...]` — NOT block-indented YAML
- `flows` **must** be a single-line YAML flow sequence: `flows: [["a", "b"], ...]`
- PyYAML requires `width=2**20` in `yaml.dump` to prevent wrapping long values — without it, descriptions with apostrophes or colons break into ugly multi-line flow style
- `page_hash` is **NOT** stored in `.md` files — it lives only in `.figma-sync/manifest.json`
- `frames` list contains node IDs only — descriptions live in the body, not frontmatter
- Node IDs containing `:` (e.g. `4713:6926`) are valid YAML values — quoted in lists

### Editing frontmatter

- **Do:** Use `figmaclaw set-flows` to update flows in frontmatter
- **Do:** Use `figmaclaw mark-enriched` to snapshot enrichment state after writing body
- **Don't:** Edit `enriched_*` fields manually — they are managed by `mark-enriched`
- **Don't:** Add `page_hash` or any legacy `figmaclaw:` nested block
- **Don't:** Put descriptions in frontmatter — they belong in the body only

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

- The body description column is **LLM-authored prose** — it is not stored in frontmatter. Frontmatter `frames` is just a list of node IDs.
- Agents and tooling **must never parse the description column** for structured data — use frontmatter for node IDs and enrichment state.
- Section intros and page summary are **only in the body** — they are not stored in frontmatter.

---

## Command responsibilities

> **Frontmatter v2 design and rationale:** [`docs/frontmatter-v2-plan.md`](frontmatter-v2-plan.md)
> **Body preservation invariants:** [`docs/body-preservation-invariants.md`](body-preservation-invariants.md)

| Command | What it does | Reads | Writes | Touches body? |
|---|---|---|---|---|
| `sync` | Fetch structure from Figma | Figma API | Frontmatter (`frames`, `flows`) + manifest | NEVER |
| `pull` | Bulk sync all tracked files | Figma API | Same as sync | NEVER |
| `write-body` | LLM writes page prose | stdin/flag | Body only, preserves frontmatter | YES |
| `mark-enriched` | Snapshot hashes as enriched | Manifest hashes | Frontmatter `enriched_*` | NO |
| `mark-stale` | Force re-enrichment | — | Clears frontmatter `enriched_*` | NO |
| `inspect` | Check structure + staleness | Frontmatter + body + manifest | Nothing | NO (read-only) |
| `set-flows` | LLM writes inferred flows | `--flows` JSON | Frontmatter `flows` only | NO |
| `screenshots` | Download frame PNGs | Manifest hash diff | `.figma-cache/` PNGs | NO |

### Why code never touches the body

The body is LLM-authored prose — page summary, section intros, Mermaid charts, filled description tables. Producing it costs Figma screenshots + LLM inference + human review. No CLI command regenerates it.

- `sync` and `pull` update frontmatter only for existing files. Body is byte-for-byte preserved.
- `write-body` is the LLM's tool for writing prose. It preserves frontmatter.
- `mark-enriched` snapshots current hashes so the system knows the body is up to date.
- The enrichment flow: `inspect → screenshots --stale → LLM → write-body → mark-enriched`.

### Enrichment detection

`inspect --needs-enrichment --json` compares frontmatter `enriched_*` fields against
manifest current hashes. Reports new/modified/removed frames in JSON output. Always
exit 0 on success — enrichment status is in the JSON, never in the exit code.

See [`docs/frontmatter-v2-plan.md`](frontmatter-v2-plan.md) for the full decision tree and design rationale.

---

---

## Parsing the format

### Reading frame IDs

```python
from figmaclaw.figma_parse import parse_frontmatter

fm = parse_frontmatter(md_text)
if fm is None:
    # No figmaclaw frontmatter or malformed YAML
    ...
frame_ids = fm.frames  # list[str]: node IDs of all frames on this page
# Descriptions live in the body tables, not in frontmatter.
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

### Enrichment flow

Descriptions live in the body only. The enrichment workflow:

```bash
figmaclaw inspect <file> --json              # check needs_enrichment
figmaclaw screenshots <file> --stale         # download only changed frames
# LLM reads screenshots, writes descriptions
figmaclaw write-body <file> <<'EOF'          # write prose body
... page summary, section intros, frame tables, Mermaid ...
EOF
figmaclaw set-flows <file> --flows '[...]'   # set inferred flows
figmaclaw mark-enriched <file>               # snapshot hashes
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
