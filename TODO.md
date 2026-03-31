# figmaclaw — TODO, Practices & Notes

**Purpose:** Turn Figma pages into AI-readable semantic markdown maps stored in git.
**Pattern:** Separate tool (own repo), same architecture as issueclaw — webhook → fetch from API → render → commit. Read-only: never write back to Figma.

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

### The Core Loop (copy from issueclaw, adapt for Figma)
```
figmaclaw apply-webhook
  → read FIGMA_WEBHOOK_PAYLOAD env var
  → validate X-Figma-Passcode
  → extract file_id
  → skip if not in tracked_files
  → call _run_pull(file_key=file_id)  ← same function as pull

_run_pull(file_key=None means all tracked)
  → for each file:
      get_file_meta(depth=1) → {version, lastModified, pages[]}
      if version == manifest.version and not --force: skip file
      for each page:
        get_page(file_key, page_node_id) → node tree
        compute_page_hash(nodes)
        if hash == manifest.page_hash: skip page
        build FigmaPage from nodes
        call LLM for any new/changed frames (batch per section)
        render_page() → markdown string
        write to figma/{file_key}/pages/{slug}.md
        update manifest page entry
      update manifest file entry
  → save manifest
  → print COMMIT_MSG: to stdout
```

### Incremental Pull — THIS IS THE KEY FEATURE
Three levels of short-circuiting:
1. **File level:** `version` + `lastModified` from depth=1 fetch. If both match manifest → skip the entire file. No page fetches.
2. **Page level:** structural hash over `(node_id, name, type, parent_id)` for all FRAME/SECTION nodes. If hash matches manifest → skip page. No LLM calls.
3. **Frame level:** within a changed page, read existing descriptions from the current `.md` file before calling LLM. Only call LLM for frames that are new or renamed. Existing unchanged frames keep their descriptions.

The goal: a nightly pull of a 10-page file with no structural changes makes **zero LLM calls** and **zero page fetches** (only 1 cheap file-meta call per file).

### Page Hash — What to Hash
Hash only structural identity, not visual style:
```python
def compute_page_hash(page_node: dict) -> str:
    tuples = []
    for child in page_node.get("children", []):
        if child["type"] in ("SECTION", "FRAME"):
            tuples.append((child["id"], child["name"], child["type"], page_node["id"]))
            for grandchild in child.get("children", []):
                if grandchild["type"] == "FRAME":
                    tuples.append((grandchild["id"], grandchild["name"], grandchild["type"], child["id"]))
    canonical = json.dumps(sorted(tuples), separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
```
NEVER hash: positions, sizes, fills, colors, fonts, styles. These change constantly and never affect the semantic map.

### Frontmatter Schema — Pydantic (unlike issueclaw which uses raw dicts)
issueclaw renders frontmatter from raw dicts. figmaclaw must use a Pydantic model:
```python
class PageMetadata(BaseModel):
    file_key: str
    file_name: str
    page_node_id: str
    page_name: str
    page_slug: str
    version: str
    page_hash: str
    last_refreshed_at: str  # ISO timestamp
    figma_url: str
```
This metadata goes in the HTML comment at the top of each .md:
```
<!-- figmaclaw: file_key=abc page_node_id=7741:45837 page_hash=a3f1b2c4 version=... -->
```
Validated at write time AND at read time (when checking for existing descriptions). If the comment is missing or malformed, treat the page as needing full regeneration.

### Tree Traversal Rules (confirmed from live Figma API)
From testing with the Web App file (`hOV4QMBnDIG5s5OYkSrX9E`):
- Top-level children of a page (CANVAS) are `SECTION` nodes — each maps to a `FigmaSection`.
- Children of a SECTION that are `FRAME` → `FigmaFrame`.
- Filter out `CONNECTOR` nodes (appear in section children, are visual connectors not screens).
- If a page has top-level `FRAME` nodes (no sections), group them into a `(Ungrouped)` section.
- **Prototype reactions are empty on most pages** — don't count on them for flow edges. Mermaid diagram is a nice-to-have, skip if no reactions. Don't fail or generate a fake diagram.

### Figma API — Key Facts (from live testing)
- Auth header: `X-Figma-Token: {api_key}` — NOT `Authorization: Bearer`.
- `GET /v1/files/{key}?depth=1` → returns name, version, lastModified, all pages. Fast.
- `GET /v1/files/{key}/nodes?ids={page_id}` → returns full tree for one page. Use this, not the full file fetch.
- `GET /v2/webhooks` → lists webhooks (team-scoped; Figma doesn't support file-scoped webhooks).
- `POST /v2/webhooks` → registers webhook at team level. All files in team trigger it; filter by `tracked_files` in apply-webhook.
- Webhook validation: check `X-Figma-Passcode` header (not HMAC like Linear).
- Token expiry: tokens expire (noted on generation), warn user in README to rotate.

### LLM Integration (Anthropic)
- Use `anthropic` Python SDK directly (not litellm, not langchain).
- Default model: `claude-haiku-4-5-20251001` (fast, cheap).
- Batch per section: one API call for all frames in a section.
- Prompt must produce JSON array of descriptions in the same order as input frame names.
- Preserve existing descriptions: before calling LLM, parse the current .md and extract existing frame descriptions by frame name. Skip LLM for frames already described.
- Respect idempotency: if the same frame gets regenerated with slightly different wording, that's a noisy git diff. Better to keep existing text unless the frame structurally changed.

### COMMIT_MSG Protocol (same as issueclaw)
Commands print `COMMIT_MSG:{message}` on stdout. GitHub Actions extracts it:
```bash
OUTPUT=$(figmaclaw apply-webhook 2>&1)
COMMIT_MSG=$(echo "$OUTPUT" | grep '^COMMIT_MSG:' | head -1 | sed 's/^COMMIT_MSG://')
```
Format: `figma: regenerate {n} page(s) in {file_name}` or `figma: {page_name} updated in {file_name}`.

### Manifest Schema (use Pydantic here too)
```python
class PageEntry(BaseModel):
    page_name: str
    page_slug: str
    md_path: str
    page_hash: str
    last_refreshed_at: str

class FileEntry(BaseModel):
    file_name: str
    version: str
    last_modified: str
    last_checked_at: str
    pages: dict[str, PageEntry] = Field(default_factory=dict)  # page_node_id → entry

class Manifest(BaseModel):
    schema_version: int = 1
    tracked_files: list[str] = Field(default_factory=list)
    files: dict[str, FileEntry] = Field(default_factory=dict)  # file_key → entry
```
Serialize with `model.model_dump_json(indent=2)`, deserialize with `Manifest.model_validate_json(text)`.

---

## Test Strategy

### Smoke Tests (run before writing any module implementation)
Before implementing figma_client.py, write a smoke test that actually hits the Figma API:
```python
# tests/smoke/test_figma_api.py
# Requires FIGMA_API_KEY env var. Skipped in CI unless explicitly enabled.
@pytest.mark.smoke
async def test_get_file_meta_returns_version():
    client = FigmaClient(api_key=os.environ["FIGMA_API_KEY"])
    meta = await client.get_file_meta("hOV4QMBnDIG5s5OYkSrX9E")
    assert meta["version"]
    assert meta["lastModified"]
    assert len(meta["document"]["children"]) > 0
```
Smoke tests are in `tests/smoke/`, require real credentials, and are skipped by default (`pytest -m "not smoke"`). Run manually to validate against real Figma before a release.

### Unit Tests — Invariant Style (like issueclaw)
Each test must document what invariant it protects:
```python
async def test_pull_skips_file_when_version_unchanged(tmp_path):
    """INVARIANT: If file version matches manifest, no page fetches happen.
    
    This is the primary cost-control mechanism. A nightly pull of an
    unchanged file must make exactly 1 API call (get_file_meta) and
    zero page fetches, zero LLM calls.
    """
```

### Integration Tests
Test the full _run_pull() loop end-to-end with mocked Figma API responses.
Use real FigmaPage/FigmaSection/FigmaFrame model instances (not raw dicts) as test data.
Verify actual .md files written to tmp_path match the expected format.

### CI Test Matrix
- `pytest -m "not smoke"` — unit + integration, runs in CI on every push
- `pytest -m smoke` — run manually or on a schedule against real Figma API
- `basedpyright` — type check on every push
- `ruff check && ruff format --check` — on every push
- `pre-commit run --all-files` — on every push

---

## Implementation Task List

### Phase 1: Core Pipeline (no LLM)

- [ ] **Repo setup**
  - [ ] `gh repo create aviadr1/figmaclaw --public --clone` → clone into ~/projects/figmaclaw
  - [ ] `pyproject.toml` with ruff, basedpyright, pytest, click, httpx, pydantic, anthropic, rich
  - [ ] `pre-commit-config.yaml` with ruff + standard hooks
  - [ ] `install.sh` script: `uv sync`, `pre-commit install`, print verification instructions
  - [ ] `.gitignore`: `.env`, `__pycache__`, `.venv`, `dist/`
  - [ ] `README.md`: what it does, install, quickstart, env vars, architecture diagram
  - [ ] Move `TODO.md` from `~/projects/figmaclaw/` into the cloned repo

- [ ] **Smoke test first**
  - [ ] `tests/smoke/test_figma_api.py` — validate all 4 endpoints before implementing client
  - [ ] Run smoke tests against live Figma to confirm behavior

- [ ] **`figmaclaw/figma_client.py`**
  - [ ] Test: retry on 429 (mock httpx response)
  - [ ] Test: retry on 5xx (mock httpx response)
  - [ ] Test: `X-Figma-Token` header used (not Bearer)
  - [ ] Test: `get_file_meta` returns version, lastModified, pages list
  - [ ] Test: `get_page` returns node tree with SECTION children
  - [ ] Implement: `FigmaClient` class with all methods

- [ ] **`figmaclaw/figma_models.py`**
  - [ ] Test: `FigmaFrame`, `FigmaSection`, `FigmaPage`, `FigmaFile` from real API response
  - [ ] Test: CONNECTOR nodes are filtered out of frame lists
  - [ ] Test: ungrouped top-level FRAMEs collected into `(Ungrouped)` section
  - [ ] Test: `from_page_node(page_dict) -> FigmaPage` traversal
  - [ ] Implement: Pydantic models + `from_page_node` classmethod

- [ ] **`figmaclaw/figma_paths.py`**
  - [ ] Test: `page_path(file_key, page_slug)` → `figma/{key}/pages/{slug}.md`
  - [ ] Test: `slugify` edge cases (unicode, special chars, multiple hyphens)
  - [ ] Implement: path conventions + slugify()

- [ ] **`figmaclaw/figma_sync_state.py`**
  - [ ] Test: `Manifest` Pydantic model round-trips through JSON
  - [ ] Test: `FigmaSyncState.load()` no-ops on missing file
  - [ ] Test: `save()` → `load()` round-trip preserves all fields
  - [ ] Test: `get_page_hash()` returns None for unknown page
  - [ ] Test: `add_tracked_file()` prevents duplicates
  - [ ] Implement: `FigmaSyncState` with Pydantic manifest

- [ ] **`figmaclaw/figma_hash.py`**
  - [ ] Test: hash is stable across identical input in different order
  - [ ] Test: hash changes when frame name changes
  - [ ] Test: hash changes when frame added
  - [ ] Test: hash does NOT change when frame position changes (visual-only)
  - [ ] Implement: `compute_page_hash(page_node: dict) -> str`

- [ ] **`figmaclaw/figma_render.py`**
  - [ ] Test: output contains `# {file_name} / {page_name}` header
  - [ ] Test: output contains HTML comment with file_key, page_hash
  - [ ] Test: output contains section table with all frames
  - [ ] Test: placeholder description used when frame has no description
  - [ ] Test: Quick Reference table present
  - [ ] Test: Mermaid block absent when no flow edges
  - [ ] Implement: `render_page(page: FigmaPage, metadata: PageMetadata) -> str`

- [ ] **`figmaclaw/commands/track.py`**
  - [ ] Test: unknown file_key raises click.UsageError
  - [ ] Test: already-tracked file warns and exits cleanly
  - [ ] Test: new file added to manifest.tracked_files
  - [ ] Implement: `figmaclaw track <file-key> [--no-pull]`

- [ ] **`figmaclaw/commands/pull.py`**
  - [ ] Test: unchanged file version → zero page fetches (mock client)
  - [ ] Test: changed version but unchanged page hash → zero LLM calls, no .md rewrite
  - [ ] Test: changed page hash → .md file written with correct content
  - [ ] Test: `--force` bypasses version check
  - [ ] Test: `--file-key` restricts to single file
  - [ ] Test: COMMIT_MSG printed to stdout with correct format
  - [ ] Implement: `_run_pull()` async core + `figmaclaw pull` click command

- [ ] **End-to-end smoke**: `figmaclaw track hOV4QMBnDIG5s5OYkSrX9E --no-pull && figmaclaw pull --file-key hOV4QMBnDIG5s5OYkSrX9E`
  - Generated .md files must match the proven format from the example
  - Manifest written to `.figma-sync/manifest.json`

### Phase 2: LLM Integration

- [ ] **`figmaclaw/figma_llm.py`**
  - [ ] Test: `generate_section_descriptions` returns list of same length as input frames
  - [ ] Test: malformed LLM JSON response raises clear error (not silent bad output)
  - [ ] Test: existing descriptions preserved for unchanged frames
  - [ ] Test: only new frames trigger LLM call (idempotency)
  - [ ] Implement: batched section prompts, idempotency, page summary, quick reference rows

- [ ] **`figmaclaw/figma_parse.py`** (read existing .md for idempotency)
  - [ ] Test: parses HTML comment metadata → `PageMetadata` Pydantic model
  - [ ] Test: parses section tables → `{frame_name: description}` dict
  - [ ] Test: returns empty dict for missing/malformed file
  - [ ] Implement: `parse_page_metadata(md_content: str) -> PageMetadata | None`
  - [ ] Implement: `parse_frame_descriptions(md_content: str) -> dict[str, str]`

- [ ] Wire LLM into pull pipeline (only called when page hash changed)
- [ ] Tune prompts against real Figma file, verify output quality

### Phase 3: Webhook + Automation

- [ ] **`figmaclaw/commands/apply_webhook.py`**
  - [ ] Test: payload with untracked file_id → skip, no API call
  - [ ] Test: invalid passcode → raises error, no API call
  - [ ] Test: valid payload → calls _run_pull with correct file_key
  - [ ] Test: COMMIT_MSG printed to stdout
  - [ ] Implement: reads FIGMA_WEBHOOK_PAYLOAD, validates, delegates to _run_pull

- [ ] **`figmaclaw/commands/init.py`**
  - [ ] Copies bundled workflow templates to .github/workflows/
  - [ ] Sets FIGMA_API_KEY, ANTHROPIC_API_KEY, FIGMA_WEBHOOK_SECRET as GitHub secrets via `gh`
  - [ ] Registers Figma FILE_UPDATE webhook via API

- [ ] **Bundled workflow templates** (in `figmaclaw/workflows/`)
  - [ ] `figmaclaw-webhook.yaml`: concurrency per file_id, cancel-in-progress: true (debounce)
  - [ ] `figmaclaw-sync.yaml`: nightly 2am UTC, manual dispatch

- [ ] **CloudFlare Worker** (separate from figmaclaw package)
  - [ ] Validate `X-Figma-Passcode` header
  - [ ] Filter to FILE_UPDATE events only
  - [ ] Forward to GitHub repository_dispatch as `figma-webhook`
  - [ ] Env vars: FIGMA_WEBHOOK_SECRET, GITHUB_TOKEN, GITHUB_REPO

### Phase 4: Production

- [ ] Add `figmaclaw-webhook.yaml` and `figmaclaw-sync.yaml` to linear-git repo
- [ ] Set FIGMA_API_KEY and ANTHROPIC_API_KEY as GitHub secrets
- [ ] Run `figmaclaw init` in linear-git
- [ ] End-to-end test: edit Figma → webhook fires → Actions run → commit appears
- [ ] Document sub-page splitting trigger (~400 line threshold)
- [ ] Update linear-git CLAUDE.md with figma/ search patterns

---

## Things Learned from issueclaw

### Do the same
- `asyncio.run()` in Click command, `async def _run_pull()` as the testable core
- Lazy-loaded caches for expensive lookups (we need this for LLM results: cache by frame name hash)
- `SyncState`-style class with explicit `load()` / `save()` (don't auto-save)
- `COMMIT_MSG:` on stdout, extracted by shell in GitHub Actions
- `git pull --no-rebase` before every apply/sync in CI
- `concurrency` group in webhook workflow to serialize runs
- `fetch-depth: 2` not needed (we don't diff git history), but `fetch-depth: 1` is fine
- `cancel-in-progress: false` for sync (don't cancel in-progress full syncs)
- `cancel-in-progress: true` for webhook (debounce: last event per file wins)
- Loop prevention: skip CI run if commit author is `figmaclaw-bot`
- `--json` flag on every command for AI-readable output
- `-v`/`-q` verbosity on the CLI group
- `click.UsageError` for validation, `click.ClickException` for runtime
- `asyncio.sleep(max(retry_after, 5))` for 429 handling
- Rich progress bars on stdout-friendly operations

### Do differently / better
- **Pydantic everywhere** — issueclaw uses raw dicts and dataclasses. figmaclaw uses Pydantic models for everything: API response models, frontmatter schema, manifest schema, diff results, parsed page data. Zero `@dataclass`. Zero naked dicts passed between functions. If it has fields, it has a Pydantic model.
- **basedpyright strict** — issueclaw doesn't use a type checker. Use it from day one.
- **ruff** — issueclaw uses basic formatting. Use ruff for both linting and formatting.
- **pre-commit** — issueclaw has none. Install it in install.sh.
- **No dataclasses** — `ParsedMarkdown`, `MarkdownDiff`, `ParsedSection` in issueclaw are all `@dataclass`. In figmaclaw every equivalent is a `BaseModel`. Pydantic gives us validation, serialization, and type safety for free.
- **Smoke tests** — issueclaw has no smoke tests against live API. Add a `tests/smoke/` directory.
- **figma_parse.py for idempotency** — issueclaw has no equivalent because Linear data is always re-rendered from scratch. figmaclaw must parse existing .md to preserve LLM descriptions.
- **No push direction** — issueclaw has push.py for bidirectional sync. figmaclaw is read-only. Don't add push — keep it simple.
- **Hash-based incremental** — issueclaw uses timestamp filtering on the Linear API. figmaclaw uses structural hashing (can't filter Figma API by timestamp on page level).

### issueclaw gaps to avoid repeating
- No type checker → use basedpyright
- No pre-commit → add it
- No smoke tests → add them
- Missing edge case tests for path parsing → test path parsing thoroughly
- No install script → add install.sh
- No schema validation for frontmatter → use Pydantic

---

## Open Questions / Decisions Needed

- [ ] **Mermaid flows**: Prototype reactions are empty on the tested page. Make flows entirely optional — skip the Mermaid block if no reactions found. Don't generate fake flows.
- [ ] **`(Ungrouped)` section name**: Is this the right label, or should we omit it and flatten ungrouped frames into the top-level table?
- [ ] **page_slug uniqueness**: What if two pages have the same slugified name? Add a counter suffix (e.g., `onboarding-2.md`)?
- [ ] **Which team_id to use for webhook registration**: The Gigaverse pro team is `team::1314617533998771588`. Confirm before registering.
- [ ] **Separator pages**: Pages named `---` or `🏞️ Cover` — skip them during pull (add a skip-list or pattern)?
- [ ] **Token expiry warning**: Figma tokens expire. Add a warning to `figmaclaw pull` output if the API returns an auth error.
- [ ] **Rate limits**: Figma API is ~100 req/min. With many pages, sequential page fetches may hit this. Add delay between page fetches if needed.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `FIGMA_API_KEY` | Yes | Figma personal access token (`figd_...`). Use `X-Figma-Token` header. |
| `FIGMA_WEBHOOK_SECRET` | Webhook only | Passcode registered with Figma webhook, validated in CF Worker |
| `ANTHROPIC_API_KEY` | Yes (LLM) | Anthropic API key for semantic description generation |
| `LLM_MODEL` | No | Override default model (`claude-haiku-4-5-20251001`) |

---

## Key File: `.figma-sync/manifest.json`
- Committed to repo — it IS the config (which files are tracked)
- Never in .gitignore
- Updated after every successful pull/apply-webhook
- Schema versioned: `schema_version: 1` — bump if format changes

## Output Directory: `figma/`
- Never manually edit files in `figma/` — they are always overwritten by figmaclaw
- `figma/{file_key}/pages/{page_slug}.md` — one file per Figma page
- Files not in the manifest are stale and can be deleted

## Tested Figma File
- **File:** Web App (`hOV4QMBnDIG5s5OYkSrX9E`)
- **32 pages**, confirmed API works for metadata + page fetch
- **Reach - auto content sharing** (`7741:45837`) — 8 SECTION nodes, all with FRAME children
- CONNECTOR nodes appear in section children — must filter
- No prototype reactions on tested page → Mermaid is optional
