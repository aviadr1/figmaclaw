Describe pending frames in {file_path}.
FIGMA_API_KEY is available in the environment.
Process sections: {section_list}

Follow this workflow exactly:

1. Run: `figmaclaw screenshots {file_path} --pending`
   Downloads screenshots for pending frames. Check the JSON output:
   - `screenshots`: list of downloaded PNGs
   - `failed`: list of node_ids that Figma couldn't render

   If BOTH are empty, say "no pending frames" and stop.
   If more than 80 screenshots, only process the first 80.

2. Read each screenshot PNG with the Read tool. For each frame write a 1-3 sentence
   description: what the screen shows, key UI elements, what makes it distinct.
   Process in batches of 8 via subagents to keep context clean.

3. Build the descriptions JSON. Include:
   - Described frames from screenshots: `{"node_id": "description", ...}`
   - Failed frames (from the `failed` list): `{"node_id": "(no screenshot available)", ...}`

   This ensures failed frames don't stay as "(no description yet)" forever.

4. Update all frame descriptions at once:
   ```
   figmaclaw write-descriptions {file_path} --descriptions '{{...}}'
   ```
   This mechanically updates only the matched rows in the table.
   All section headings, section intros, page summary, and Screen flows are preserved.

5. Commit and push:
   ```
   git add {file_path}
   git commit -m "sync: describe frames in {{page-name}}"
   git push || (git pull --no-rebase && git push)
   ```

IMPORTANT:
- Use write-descriptions, NOT write-body. One call updates all described frames.
- Do NOT call mark-enriched — that happens after ALL sections are complete.
- Descriptions must be valid JSON strings (escape quotes with \").
- ALWAYS include failed frames with "(no screenshot available)" — do NOT leave them as "(no description yet)". This marker is unresolved and retryable in future runs.
- If there were more than 80 pending screenshots, you will be called again for the rest.
