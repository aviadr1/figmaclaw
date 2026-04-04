# Failure Postmortem: Enrichment Pipeline (2026-04-03 to 2026-04-04)

## Timeline of failures caused during this work session

### 1. Original bug (pre-existing): Syntax error in claude_run.py
- **When:** Before session, running for 24+ hours
- **What:** Orphaned `except` block in `scripts/claude_run.py` (linear-git)
- **Impact:** All CI enrichment broken. 20+ consecutive failures.
- **Root cause:** Refactor left behind dead code. No tests for the script.
- **Fix:** Removed the orphaned line. Added syntax validation tests.
- **Lesson:** Scripts need tests. py_compile as a canary test.

### 2. ModuleNotFoundError: figmaclaw not importable
- **When:** 2026-04-03 ~09:07 UTC
- **What:** Ported claude_run.py to figmaclaw as a bundled script, used `importlib.resources` to locate it. But `uv tool install` creates an isolated venv — module not importable from system Python.
- **Impact:** First enrichment run after fix still failed.
- **Root cause:** Wrong approach — tried to import from a CLI tool's isolated environment.
- **Fix:** Initially used `pip install` (hacky). Then properly converted to Click CLI commands (`figmaclaw claude-run`, `figmaclaw stream-format`).
- **Lesson:** CLI tools should be called as CLI commands, not imported as libraries.

### 3. Re-enrichment race condition
- **When:** 2026-04-03 ~11:00-12:00 UTC
- **What:** Overlapping CI runs re-enriched ~16 files that were already done.
- **Impact:** ~$0.50 wasted, ~16 minutes of redundant work.
- **Root cause:** Each CI run checks out code at start time. If run B starts while run A is mid-enrichment, B's checkout doesn't have A's committed enrichments.
- **Fix:** Added `git pull` before each file to get latest state.
- **Lesson:** CI runs that write back to the repo must handle concurrent writers.

### 4. Concurrency group collision (first instance)
- **When:** 2026-04-03 ~14:00-16:00 UTC
- **What:** `enrich` and `enrich-large` jobs cancelled each other because they shared the same concurrency key (`claude-run-refs/heads/main-figma/`).
- **Impact:** `enrich-large` only ran successfully ONCE before being cancelled in every subsequent run.
- **Root cause:** Both jobs had `target: figma/` and no distinguishing key in the concurrency group.
- **Fix:** Added `section_mode` to the concurrency key: `claude-run-...-${{ inputs.section_mode && 'sections' || 'bulk' }}`.
- **Lesson:** Reusable workflows with `cancel-in-progress: true` need unique concurrency keys per caller.

### 5. Missing `section_mode: true` in caller workflow
- **When:** 2026-04-03 ~17:00 UTC to 2026-04-04 ~05:00 UTC (~10 hours)
- **What:** When reverting from the 500-frame threshold back to 80, forgot to add `section_mode: true` to the `enrich-large` job definition.
- **Impact:** The concurrency fix (failure #4) was negated — both jobs evaluated to `bulk` in the concurrency key, so `enrich-large` was cancelled every run for 10 hours. Zero large-page enrichment during this time.
- **Root cause:** Manual edit error when updating the workflow file. No validation that the concurrency keys actually differ.
- **Fix:** Added the missing line.
- **Lesson:** Test workflow changes by verifying BOTH jobs actually run in the next CI cycle before moving on.

### 6. Duplicate YAML key broke workflow entirely
- **When:** 2026-04-04 ~05:10 UTC to ~06:28 UTC
- **What:** The fix for failure #5 accidentally added `section_mode: true` twice (duplicate YAML key). GitHub Actions rejected the entire workflow file.
- **Impact:** NO enrichment at all (neither small nor large pages) for ~1.5 hours.
- **Root cause:** Editing the file added the line but the line was already there from a previous edit, creating a duplicate. No local YAML validation before push.
- **Fix:** Removed duplicate line. Added local YAML validation.
- **Lesson:** Always validate YAML locally before pushing workflow changes. `python -c "import yaml; yaml.safe_load(open('...'))"`.

### 7. MAX_FRAMES_PER_FILE import error in tests
- **When:** 2026-04-03, during initial refactor
- **What:** Removed the `MAX_FRAMES_PER_FILE` constant but tests still imported it.
- **Impact:** Tests broken until fixed. Not caught before pushing because tests weren't run.
- **Root cause:** Incomplete refactor — changed the source but not the tests.
- **Fix:** Updated tests to use `max_frames` parameter directly.
- **Lesson:** Run the full test suite before pushing.

## Common patterns

1. **"Fix it fast, break it twice"** — Failures #4, #5, #6 are a cascade. Each fix for the previous failure introduced the next one. Slowing down to verify each fix would have prevented the cascade.

2. **No local validation** — Failures #2, #6, #7 could have been caught before pushing: import test, YAML validation, test suite run.

3. **Not verifying CI after push** — Failures #5 and #6 persisted for hours because I moved on without confirming the fix worked in CI.

4. **Manual workflow edits are error-prone** — Failures #5 and #6 were both hand-edit mistakes in YAML. A templating system or at least a diff review would catch these.

## Prevention checklist (for future changes)

- [ ] Run `uv run pytest -x` before every push to figmaclaw
- [ ] Run `python -c "import yaml; yaml.safe_load(open('.github/workflows/...'))"` before pushing workflow changes
- [ ] After pushing a workflow change, wait for the NEXT CI run and verify ALL jobs start and run (don't just check one)
- [ ] After fixing a concurrency issue, verify the concurrency keys evaluate to DIFFERENT values by checking the GitHub Actions UI
- [ ] Never amend/re-edit a file that was just pushed — make a fresh clean edit and diff it
