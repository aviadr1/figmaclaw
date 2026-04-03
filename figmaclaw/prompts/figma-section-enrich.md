Enrich section `{section_node_id}` ("{section_name}") in the figmaclaw page file at {file_path}.
FIGMA_API_KEY is available in the environment.

This is ONE section of a large page. Enrich ONLY this section — do NOT touch other
sections, the page summary, or Screen flows. Do NOT call mark-enriched.

Follow this workflow exactly:

1. Run: `figmaclaw inspect {file_path} --json`
   Find the section with node_id `{section_node_id}` in the sections array.
   If that section has `pending_frames: 0` and `stale_frames: 0`, say "section already
   enriched" and stop.

2. Read the existing file to see the current section structure:
   ```
   cat {file_path}
   ```
   Note the section heading, any existing intro, and the frame table.

3. Run: `figmaclaw screenshots {file_path} --section {section_node_id} --pending`
   Downloads screenshots only for pending frames in this section.

4. Read each screenshot PNG with the Read tool. For each frame write a 1-3 sentence
   description: what the screen shows, key UI elements, what makes it distinct.
   Process screenshots in batches of 8 via subagents to keep context clean.

5. Write ONLY this section via `figmaclaw write-body --section {section_node_id}`:
   ```
   figmaclaw write-body {file_path} --section {section_node_id} <<'EOF'
   ## {section_name} (`{section_node_id}`)

   {{1-sentence section intro}}

   | Screen | Node ID | Description |
   |--------|---------|-------------|
   | {{frame_name}} | `{{node_id}}` | {{description}} |
   EOF
   ```

6. Commit and push:
   ```
   git add {file_path}
   git commit -m "sync: describe {section_name} section in {{page-name}}"
   git push || (git pull --no-rebase && git push)
   ```

IMPORTANT:
- Write ONLY the single section specified. Other sections, the page summary, and
  Screen flows are preserved automatically by `write-body --section`.
- The section text you write must include the `## ` header line, the section intro,
  and the full frame table.
- Do NOT call mark-enriched — that happens after ALL sections are complete.
- Descriptions live in the body only. Use write-body, not set-frames.
