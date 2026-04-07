---
description: Enrich a figmaclaw Figma page .md file with frame descriptions, page summary, and screen flows from design inspection.
---

# figma-enrich-page

Enrich a figmaclaw `.md` file with frame descriptions, page summary, section intros, and Mermaid flowchart.

**Full workflow:** `figmaclaw self skill figma-enrich-page` — authoritative skill file.

## Quick flow

```bash
figmaclaw inspect <file> --json           # check needs_enrichment
figmaclaw screenshots <file> --stale      # download PNGs for pending frames
# LLM describes frames from screenshots
figmaclaw write-body <file> <<'EOF'         # write body prose
... descriptions, summary, intros, Mermaid ...
EOF
figmaclaw set-flows <file> --flows '[...]'  # set inferred flows
figmaclaw mark-enriched <file>              # snapshot hashes
```

## Key rules

- Descriptions live in the **body** only. Use `write-body`, not `set-frames`.
- `mark-enriched` tells the system the body is up-to-date with current Figma structure.
- Always include `## Screen flows` — look at screenshots for transitions.
- Never parse body prose for structured data — read frontmatter.
