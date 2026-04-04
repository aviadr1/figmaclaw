Describe pending frames in {file_path}.
FIGMA_API_KEY is available in the environment.
Process sections: {section_list}

Follow this workflow exactly:

1. Run: `figmaclaw screenshots {file_path} --pending`
   Downloads screenshots for ALL pending frames across the listed sections.
   If no screenshots downloaded, say "no pending frames" and stop.
   If more than 80 screenshots, only process the first 80.

2. Read each screenshot PNG with the Read tool. For each frame write a 1-3 sentence
   description: what the screen shows, key UI elements, what makes it distinct.
   Process in batches of 8 via subagents to keep context clean.

3. Update all frame descriptions at once:
   ```
   figmaclaw write-descriptions {file_path} --descriptions '{{"node_id_1": "description 1", "node_id_2": "description 2", ...}}'
   ```
   This mechanically updates only the matched rows in the table.
   All section headings, section intros, page summary, and Screen flows are preserved.

4. Commit and push:
   ```
   git add {file_path}
   git commit -m "sync: describe frames in {{page-name}}"
   git push || (git pull --no-rebase && git push)
   ```

IMPORTANT:
- Use write-descriptions, NOT write-body. One call updates all described frames.
- Do NOT call mark-enriched — that happens after ALL sections are complete.
- Descriptions must be valid JSON strings (escape quotes with \").
- If there were more than 80 pending screenshots, you will be called again for the rest.
