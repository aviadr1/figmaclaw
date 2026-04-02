---
description: Enrich figmaclaw .md files with frame descriptions. Use when a page has (no description yet) placeholders or needs_enrichment is true.
---

# figmaclaw/enrich-page

Enrich one or more figmaclaw `.md` files with frame descriptions.

**Full workflow:** run `figmaclaw self skill figma-enrich-page` to print the authoritative skill file.

## Quick summary

```bash
# 1. Check what needs work
figmaclaw page-tree <md-path> --json  # check needs_enrichment field

# 2. Download screenshots
figmaclaw screenshots <md-path> --pending

# 3. LLM generates descriptions from screenshots

# 4. Write body prose
figmaclaw write-body <md-path> <<'EOF'
... full body with descriptions, summary, intros, Mermaid ...
EOF

# 5. Set flows (if inferred from screenshots)
figmaclaw set-flows <md-path> --flows '[["src", "dst"], ...]'

# 6. Mark as enriched
figmaclaw mark-enriched <md-path>

# 7. Verify
figmaclaw page-tree <md-path> --json  # needs_enrichment should be false

# 8. Commit
git add <md-path> .figma-cache/ .figma-sync/
git commit -m "sync: describe <page-name> frames"
git push || (git pull --no-rebase && git push)
```

## Key rules

- Descriptions live in the **body** only (not frontmatter). Use `write-body`, not `set-frames`.
- `set-flows` writes flows to frontmatter. `write-body` writes prose to body.
- `mark-enriched` snapshots hashes so the system knows the body is up-to-date.
- Body tables are LLM-authored prose — code never touches them.
- Always include `## Screen flows` — look at screenshots for transitions.
