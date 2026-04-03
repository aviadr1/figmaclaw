Finalize enrichment for the figmaclaw page file at {file_path}.
All sections have been enriched individually. Now write the page summary and Screen flows.
FIGMA_API_KEY is available in the environment.

Follow this workflow exactly:

1. Run: `figmaclaw inspect {file_path} --json`
   Verify `pending_sections` is 0 and `stale_sections` is 0.
   If not, stop — sections still need enrichment.

2. Read the full file:
   ```
   cat {file_path}
   ```
   All section intros and frame descriptions should be filled in.

3. Download ALL screenshots (needed for flow analysis):
   ```
   figmaclaw screenshots {file_path}
   ```

4. Write the complete body via `figmaclaw write-body`:
   - Preserve every section intro and frame description table exactly as they are
   - Write/update the 2-3 sentence **page summary** between the `[Open in Figma]` link
     and the first `##` heading
   - Write/update the **Screen flows** Mermaid block at the end — look at screenshots
     for transitions (buttons, CTAs, step indicators, modals)

   ```
   figmaclaw write-body {file_path} <<'EOF'
   # {{file_name}} / {{page_name}}

   [Open in Figma]({{figma_url}})

   {{2-3 sentence page summary}}

   {{ALL section blocks exactly as they currently appear — copy verbatim}}

   ## Screen flows

   ```mermaid
   flowchart LR
       A["screen"] -->|action| B["next screen"]
   ` ``
   EOF
   ```

5. If you identified flows from screenshots:
   ```
   figmaclaw set-flows {file_path} --flows '[["src_id", "dst_id"], ...]'
   ```

6. Mark as enriched:
   ```
   figmaclaw mark-enriched {file_path}
   ```

7. Verify: `figmaclaw inspect {file_path} --json` — `needs_enrichment` should be false.

8. Commit and push:
   ```
   git add {file_path} .figma-cache/ .figma-sync/
   git commit -m "sync: finalize enrichment for {{page-name}}"
   git push || (git pull --no-rebase && git push)
   ```

IMPORTANT:
- Do NOT rewrite section descriptions — copy them verbatim from the existing file.
- Only write/update the page summary and Screen flows mermaid block.
- mark-enriched must be called — this snapshots the current hashes so the system
  knows the page is fully up-to-date.
