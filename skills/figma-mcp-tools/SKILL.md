---
name: Figma MCP Tools
description: Use when working with Figma MCP tools — looking up design tokens/variables, inspecting components, or deciding which MCP tool to call. Covers which tools require an active Figma selection vs. work headlessly, and the correct lookup order for tokens and components.
---

# Figma MCP Tools Reference

## Tool constraints — selection required vs. headless

| Tool | Requires active selection? | Notes |
|---|---|---|
| `get_variable_defs` | **YES** — fails if nothing selected in Figma desktop | Do not call with just a node ID; the user must have the layer selected |
| `get_design_context` | No — node ID is sufficient | Primary tool for inspecting a specific node |
| `search_design_system` | No | Best headless tool for browsing components and variables |
| `get_metadata` | No | File/page metadata without selection |
| `get_screenshot` | No | Frame screenshot by node ID |
| `get_figjam` | No | FigJam boards |
| `use_figma` | No (writes via plugin API) | Executes JS in Figma plugin context |

**Never try `get_variable_defs` as a first step.** It always fails unless the user is actively working in Figma with a layer selected.

---

## Token / variable lookup order

Figma is the source of truth for design tokens. CSS files, JS exports, or any other derived artifacts are downstream — never treat them as authoritative.

1. **Read committed `.md` files first** — consumer repos sync Figma data to git. Check `figma/design-system/` for a `_census.md` or variable export pages. Zero MCP calls needed.
2. **`search_design_system` with `includeVariables: true`** — headless, works without Figma open:
   ```
   search_design_system(query="color surface background", fileKey="<ds-file-key>", includeStyles=true, includeVariables=true)
   search_design_system(query="spacing radius typography", fileKey="<ds-file-key>", includeVariables=true)
   ```
3. **`get_design_context` on a specific node** — when you have a node ID from frontmatter and need exact token bindings on that node.
4. **`get_variable_defs`** — only if the user confirms they have the relevant layer selected in Figma desktop.

---

## Component lookup order

1. **Read `figma/design-system/_census.md`** if it exists — has every published component set with name, key, and page.
2. **`search_design_system`** (headless):
   ```
   search_design_system(query="button avatar badge", fileKey="<ds-file-key>", includeComponents=true)
   ```
   Only results whose `libraryName` matches the primary DS are current — ignore deprecated libraries.
3. **`get_design_context` on source frames** — for visual spec (colors, spacing, fonts, embedded DS instances). Call on the specific frame node ID, not the page root.
4. **Drill into leaf nodes for icons** — `get_design_context` on a parent frame returns sparse metadata. Call it on the specific icon instance node to get the Font Awesome ligature name and background color.

---

## Correct workflow for token audit / DS inspection

```
1. Read figma/**/*.md frontmatter → get file_key, frame node IDs
2. search_design_system(includeVariables=true) → full token inventory, headless
3. get_design_context(nodeId=<frame>) → real colors/spacing on specific nodes
4. Only reach for get_variable_defs if user confirms layer is selected
```

Do not invert this order. Attempting `get_variable_defs` first, then falling back to reading CSS files, is the wrong path.
