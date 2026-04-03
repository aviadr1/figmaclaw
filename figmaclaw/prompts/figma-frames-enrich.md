Describe frames in {file_path}, section `{section_node_id}` ("{section_name}").
FIGMA_API_KEY is available in the environment.

This is a BATCH of frames from a large section. Describe ONLY the pending frames,
then update them using write-descriptions. Do NOT rewrite the section or call write-body.

Follow this workflow exactly:

1. Run: `figmaclaw screenshots {file_path} --section {section_node_id} --pending`
   Downloads screenshots only for frames that still need descriptions.
   If no screenshots downloaded, say "no pending frames" and stop.

2. Read each screenshot PNG with the Read tool. For each frame write a 1-3 sentence
   description: what the screen shows, key UI elements, what makes it distinct.
   Process up to 40 screenshots in this invocation. If there are more than 40,
   describe the first 40 and stop — you will be called again for the remainder.
   Use subagent batches of 8 to keep context clean.

3. Update the frame descriptions:
   ```
   figmaclaw write-descriptions {file_path} --descriptions '{"node_id_1": "description 1", "node_id_2": "description 2", ...}'
   ```
   This mechanically updates only the matched rows in the table.
   All other rows, section intros, page summary, and Screen flows are preserved.

4. Commit and push:
   ```
   git add {file_path}
   git commit -m "sync: describe {section_name} frames in {{page-name}}"
   git push || (git pull --no-rebase && git push)
   ```

IMPORTANT:
- Do NOT call write-body or write-body --section. Use write-descriptions only.
- Do NOT call mark-enriched — that happens after ALL sections are complete.
- Descriptions must be valid JSON strings (escape quotes with \").
- If there are many pending frames, you may be called multiple times for the
  same section — each call handles a batch. That's expected.
