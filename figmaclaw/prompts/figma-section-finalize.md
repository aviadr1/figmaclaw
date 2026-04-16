Finalize enrichment for the figmaclaw page file at {file_path}.
All frame descriptions are complete. Now add page summary, section intros, screen flows, and mark as enriched.
FIGMA_API_KEY is available in the environment.

Follow this workflow exactly:

1. Run: `figmaclaw inspect {file_path} --json`
   Verify `pending_sections` is 0. If not, stop — frames still need descriptions.

2. Read the full file to understand the page content:
   ```
   cat {file_path}
   ```

3. Download all screenshots for flow analysis:
   ```
   figmaclaw screenshots {file_path}
   ```

4. For each section that lacks an intro (no text between the `## ` heading and the
   `| Screen |` table header), write a 1-sentence intro using the safe `--intro` flag:
   ```
   figmaclaw write-body {file_path} --section <section_node_id> --intro "One-sentence intro describing what this group of screens covers."
   ```
   This inserts the intro WITHOUT touching the frame table. Safe for any section size.
   Do this for every section that needs an intro.

5. Write the page summary. Insert a 2-3 sentence summary between the `[Open in Figma]`
   link and the first `##` heading. Use the Bash tool with sed or the Edit tool to insert
   the summary line. Do NOT use `write-body` (full replace) — too risky for large pages.

6. If you identified screen flows from the screenshots:
   ```
   figmaclaw set-flows {file_path} --flows '[["src_id", "dst_id"], ...]'
   ```

7. Mark as enriched:
   ```
   figmaclaw mark-enriched {file_path}
   ```

8. Commit and push:
   ```
   git add {file_path}
   git commit -m "sync: finalize enrichment for {{page-name}}"
   git push || (git pull --no-rebase && git push)
   ```

IMPORTANT:
- Use `write-body --section --intro` for section intros — it NEVER touches frame tables.
- Do NOT use `write-body` (full replace) — it risks truncating large tables.
- The critical step is mark-enriched — call it even if some intros fail.
- If push is rejected, use ONLY: `git pull --no-rebase && git push`
- NEVER use `git stash`, `git stash pop`, `git reset --hard`, `git checkout --`, or `rm` for recovery.
- NEVER delete `.figma-sync/*` files to make git/push succeed.
