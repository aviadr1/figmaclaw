// ── In-context usage section ───────────────────────────────────────────────────
// Builds a composite "Usage in Context" frame from individual sections of a real
// product screen. Each section is placed as either live SVG nodes or a PNG image
// fill, depending on its compressed size.
//
// ── Architecture (read this first) ────────────────────────────────────────────
//
// The MCP plugin sandbox has NO network access and NO clientStorage. The only
// way to get external data into the plugin is to embed it directly in the
// use_figma code string (50K char limit per call).
//
// Because a full screen SVG is ~2MB (too large), we split the screen into
// sections and make ONE use_figma call per section. Each call embeds that
// section's pre-fetched data (SVG string or base64 PNG).
//
// The orchestration (fetching, compressing, sequencing calls) is done by
// figmaclaw server-side Python. This file contains only the plugin-side helpers.
//
// ── Section decision rule ─────────────────────────────────────────────────────
//
//   For each section of the source frame:
//     1. Export as SVG via REST API → compress with svgo
//     2. If compressed SVG ≤ 38,000 chars → embed SVG → createNodeFromSvg
//        Result: real editable Figma vector nodes (pixel perfect, live)
//     3. Else → try raster (PNG then JPG) at scales [0.5, 0.35, 0.25] in order
//        Use first format+scale whose base64 fits under 38,000 chars
//        Result: flat raster image (not editable; JPG preferred for photos)
//   Overhead per call: ~10,200 chars (helpers + boilerplate), budget = 50,000 - 10,200.
//
//   Sections with complex fills / embedded avatar images are PNG candidates.
//   Simple sections (text + shapes only) compress well enough for SVG.
//
// ── Insights Tab section map (file 6nAmiusEvU31Z3fosx6vuo, frame 7424:15980) ──
//
//   Node ID      | Name             | y    | w×h      | Approach
//   -------------|------------------|------|----------|----------------
//   7424:16018   | Header           |    0 | 393×116  | SVG  (11.1KB)
//   7424:17814   | Top Questions    |  132 | 361×252  | PNG  (17.1KB b64)
//   7429:19265   | Top Comments     |  404 | 361×252  | PNG  (16.9KB b64)
//   7429:19382   | Top Reactions    |  676 | 361×252  | PNG  (17.5KB b64)
//   7429:19718   | Pinned Messages  |  948 | 361×252  | PNG  (11.7KB b64)
//   7429:19837   | Trending Topics  | 1220 | 361×198  | SVG  (39.5KB)
//   7429:104446  | Bottom bar       | 1434 | 393×116  | PNG   (3.7KB b64)
//
//   Frame total: 393×1584. Background color: #0D0D0D (dark).
//   Sections at x=16 are inset from the frame edge. Header and bottom bar at x=0.
//
// ── Call sequence (figmaclaw Python orchestrates) ─────────────────────────────
//
//   Call 0  — createContextContainer  → returns container frame node ID
//   Call 1  — placeContextSection (Header, SVG)
//   Call 2  — placeContextSection (Top Questions, PNG)
//   Call 3  — placeContextSection (Top Comments, PNG)
//   Call 4  — placeContextSection (Top Reactions, PNG)
//   Call 5  — placeContextSection (Pinned Messages, PNG)
//   Call 6  — placeContextSection (Trending Topics, SVG)
//   Call 7  — placeContextSection (Bottom bar, PNG)
//
//   Each call after 0 finds the container frame by name and appends to it.
//
// ── API key ───────────────────────────────────────────────────────────────────
//
//   Figma REST API calls happen server-side (figmaclaw Python), not here.
//   Read the API key from linear-git/.env → FIGMA_API_KEY at call time.
//   Never store or commit the key.
//
// ─────────────────────────────────────────────────────────────────────────────


// Call 0 — createContextContainer
// ─────────────────────────────────────────────────────────────────────────────
// Creates the outer frame that holds all sections. Call once before the section
// placement calls. Returns the frame node ID so subsequent calls can find it.
//
// Parameters:
//   page      — PageNode (must already be current page)
//   w, h      — source frame dimensions (393, 1584 for Insights Tab)
//   x, y      — canvas position (x = comp.x + comp.width + 60, y = comp.y)
//   name      — unique name used by subsequent calls to find this frame
//               e.g. 'ctx-insights-tab-7424-15980'
//   bgColor   — background fill color {r,g,b} (default dark: {r:0.05,g:0.05,b:0.05})
//
// Example:
//   const frame = createContextContainer(page, 393, 1584,
//     comp.x + comp.width + 60, comp.y,
//     'ctx-insights-tab-7424-15980');
//   return frame.id;  // pass to subsequent calls
//
function createContextContainer(page, w, h, x, y, name, bgColor) {
  const bg = bgColor ?? { r: 0.051, g: 0.051, b: 0.051 };
  const frame = figma.createFrame();
  frame.name = name;
  frame.resize(w, h);
  frame.x = x;
  frame.y = y;
  frame.fills = [{ type: 'SOLID', color: bg }];
  frame.clipsContent = true;
  frame.cornerRadius = 12;
  page.appendChild(frame);
  return frame;
}


// Calls 1–N — placeContextSection
// ─────────────────────────────────────────────────────────────────────────────
// Places one section inside the container frame. Call once per section.
// The container frame is looked up by name (containerName) on the current page.
//
// Parameters:
//   page          — PageNode (must already be current page)
//   containerName — name passed to createContextContainer
//   section       — object describing the section:
//     {
//       type:   'svg' | 'png' | 'jpg',
//       data:   string,   // SVG markup OR base64-encoded raster bytes
//       x:      number,   // x position relative to container frame (0 or 16)
//       y:      number,   // y position relative to container frame
//       w:      number,   // original section width in Figma units
//       h:      number,   // original section height in Figma units
//       name:   string,   // descriptive name e.g. 'Header', 'Trending Topics'
//     }
//
// Example (SVG section — figmaclaw injects the svgString at call time):
//   const SVG_DATA = `<svg ...>...</svg>`;  // injected by figmaclaw
//   await placeContextSection(page, 'ctx-insights-tab-7424-15980', {
//     type: 'svg', data: SVG_DATA,
//     x: 0, y: 0, w: 393, h: 116, name: 'Header'
//   });
//
// Example (PNG section — figmaclaw injects the base64 string at call time):
//   const PNG_B64 = 'iVBORw0KGgo...';  // injected by figmaclaw (base64, no data: prefix)
//   await placeContextSection(page, 'ctx-insights-tab-7424-15980', {
//     type: 'png', data: PNG_B64,
//     x: 16, y: 132, w: 361, h: 252, name: 'Top Questions'
//   });
//
async function placeContextSection(page, containerName, section) {
  // Find container
  const container = page.children.find(n => n.name === containerName && n.type === 'FRAME');
  if (!container) throw new Error(`Container frame '${containerName}' not found on current page`);

  let node;

  if (section.type === 'svg') {
    node = figma.createNodeFromSvg(section.data);
    node.name = section.name;
    node.x = section.x;
    node.y = section.y;
    // createNodeFromSvg returns a FRAME sized to the SVG viewport
    // Resize to match original Figma dimensions if they differ
    if (Math.abs(node.width - section.w) > 1 || Math.abs(node.height - section.h) > 1) {
      node.resize(section.w, section.h);
    }
    container.appendChild(node);

  } else {
    // PNG: decode base64 → create image → rect fill
    const bytes = figma.base64Decode(section.data);
    const img = figma.createImage(bytes);
    node = figma.createRectangle();
    node.name = section.name;
    node.resize(section.w, section.h);
    node.x = section.x;
    node.y = section.y;
    node.fills = [{ type: 'IMAGE', scaleMode: 'FILL', imageHash: img.hash }];
    container.appendChild(node);
  }

  return node.id;
}


// Optional: addContextCaption
// ─────────────────────────────────────────────────────────────────────────────
// Adds a small label below the container frame. Call after all sections placed.
//
// Example:
//   await figma.loadFontAsync({ family: 'Inter', style: 'Regular' });
//   addContextCaption(page, 'ctx-insights-tab-7424-15980',
//     'Mobile Insights Tab — Community in Live');
//
async function addContextCaption(page, containerName, labelText) {
  const container = page.children.find(n => n.name === containerName && n.type === 'FRAME');
  if (!container) return;

  await figma.loadFontAsync({ family: 'Inter', style: 'Regular' });
  const cap = figma.createText();
  cap.fontName = { family: 'Inter', style: 'Regular' };
  cap.fontSize = 10;
  cap.characters = labelText;
  cap.fills = [{ type: 'SOLID', color: { r: 0.45, g: 0.45, b: 0.45 } }];
  cap.x = container.x;
  cap.y = container.y + container.height + 8;
  page.appendChild(cap);
  return cap;
}
