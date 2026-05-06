Enrich the figmaclaw page file at {file_path}.
FIGMA_API_KEY is available in the environment.

Follow this workflow exactly:

1. Run: `figmaclaw inspect {file_path} --json`
   Check `needs_enrichment` in the JSON. If false, say "already enriched" and stop.

2. Run: `figmaclaw screenshots {file_path} --stale` to download screenshots.

3. Read each screenshot PNG with the Read tool. For each frame write a 1-3 sentence
   description: what the screen shows, key UI elements, what makes it distinct.
   Process screenshots in batches of 8 via subagents to keep context clean.

4. Write the full body via `figmaclaw write-body`:
   ```
   figmaclaw write-body {file_path} <<'EOF'

   # {{file_name}} / {{page_name}}

   [Open in Figma]({{figma_url}})

   {{2-3 sentence page summary}}

   ## {{Section Name}} (`{{section_node_id}}`)

   {{1-sentence section intro}}

   | Screen | Node ID | Description |
   |--------|---------|-------------|
   | {{frame_name}} | `{{node_id}}` | {{description}} |

   ## Screen flows

   ```mermaid
   flowchart LR
       A["screen"] -->|action from design| B["next screen"]
   ` ``
   EOF
   ```
   Look at screenshots for transitions — buttons, CTAs, step indicators.

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
   git add {file_path}
   git commit -m "sync: describe {{page-name}} frames"
   git push
   ```

IMPORTANT:
- Descriptions live in the **body** only. Use `write-body`, not `set-frames`.
- `mark-enriched` tells the system the body is current. Always call it after `write-body`.
- Commit and push after EVERY file. Never batch commits.
- If push is rejected, stop and report the rejected push. Do not merge or rewrite generated artifacts as recovery.
- NEVER use `git stash`, `git stash pop`, `git reset --hard`, `git checkout --`, or `rm` for recovery.
- NEVER delete `.figma-sync/*` files to make git/push succeed.
