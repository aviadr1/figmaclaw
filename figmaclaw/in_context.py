"""Build composite "Usage in Context" frames in Figma DS component draft pages.

Orchestrates a sequence of use_figma calls that place a real product screen
next to a DS component set. Each call embeds pre-fetched section data (SVG or
PNG base64) within the 50K code string limit.

Architecture
------------
The Figma MCP plugin sandbox has no network access and no clientStorage.
The only data entry point is the use_figma code string (≤50,000 chars).

Strategy: split the source frame into sections, one use_figma call per section.
  - SVG preferred (live editable nodes): use if compressed SVG ≤ SVG_SIZE_LIMIT
  - PNG fallback (flat image fill): base64-encoded PNG @scale=0.25, always fits

Section positions are read from figma page frontmatter (frame_sections field,
written by the pull pass). No extra REST API call needed at build time.

See figmaclaw issues #35 and #38.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_frontmatter import SectionNode

# Max data string length that safely fits inside a use_figma code call alongside helpers.
# Measured overhead per call: helpers (~9.8KB) + _find_page_js (~165 chars)
# + placeContextSection boilerplate (~175 chars) ≈ 10_200 chars total.
# Budget: 50_000 - 10_200 = 39_800. Use 38_000 for safety headroom.
SVG_SIZE_LIMIT = 38_000

# PNG export scale. 0.25 renders a 393×854 section at ~98×213px (~16KB PNG / ~21KB base64).
# Chosen to stay safely under SVG_SIZE_LIMIT for photo-heavy sections.
# (0.35 scale was too large: a 393×854px section produced a 32KB PNG / 43KB base64.)
PNG_SCALE = 0.25

# Plugin helpers JS — pasted at the top of every use_figma code string.
_HELPERS_PATH = Path(__file__).parent / "plugin" / "in-context.js"
_HELPERS_JS: str = _HELPERS_PATH.read_text()


@dataclass
class SectionData:
    """Pre-fetched data for one section of the source frame."""
    section: SectionNode
    kind: str    # 'svg' or 'png'
    data: str    # SVG markup or base64-encoded PNG bytes (no data: URI prefix)


async def fetch_section_data(
    client: FigmaClient,
    file_key: str,
    section: SectionNode,
) -> SectionData:
    """Fetch SVG or PNG for one section.

    Tries SVG first. Falls back to PNG @scale=0.25 if SVG exceeds SVG_SIZE_LIMIT.
    PNG always fits within the use_figma 50K code string limit at this scale.
    """
    svg_urls = await client.get_image_urls(file_key, [section.node_id], format="svg")
    svg_url = svg_urls.get(section.node_id)
    if svg_url:
        svg_bytes = await client.download_url(svg_url)
        svg_str = svg_bytes.decode("utf-8")
        if len(svg_str) <= SVG_SIZE_LIMIT:
            return SectionData(section=section, kind="svg", data=svg_str)

    # SVG too large — fall back to PNG
    png_urls = await client.get_image_urls(
        file_key, [section.node_id], format="png", scale=PNG_SCALE
    )
    png_url = png_urls.get(section.node_id)
    if not png_url:
        raise ValueError(
            f"No export URL returned for section {section.node_id!r} ({section.name!r})"
        )
    png_bytes = await client.download_url(png_url)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return SectionData(section=section, kind="png", data=b64)


def make_context_calls(
    *,
    target_file_key: str,
    target_page_id: str,
    container_name: str,
    frame_w: int,
    frame_h: int,
    comp_x: int,
    comp_y: int,
    comp_w: int,
    label: str,
    section_data_list: list[SectionData],
) -> list[dict[str, str]]:
    """Generate the ordered sequence of use_figma call specs.

    Returns a list of dicts: [{"file_key": ..., "description": ..., "code": ...}, ...]
    Each dict maps directly to the use_figma tool parameters.
    Execute them in order.

    Call 0:   createContextContainer — creates the outer frame
    Calls 1-N: placeContextSection — one per section
    Final:    addContextCaption — caption below the frame
    """
    x = comp_x + comp_w + 60
    y = comp_y

    calls: list[dict[str, str]] = []

    # Call 0: create container frame
    calls.append({
        "file_key": target_file_key,
        "description": f"Create context container frame '{container_name}' on page {target_page_id}",
        "code": _container_code(target_page_id, frame_w, frame_h, x, y, container_name),
    })

    # Calls 1-N: place each section
    for sd in section_data_list:
        calls.append({
            "file_key": target_file_key,
            "description": f"Place {sd.section.name!r} section ({sd.kind}) in '{container_name}'",
            "code": _section_code(target_page_id, container_name, sd),
        })

    # Final: add caption
    calls.append({
        "file_key": target_file_key,
        "description": f"Add caption to '{container_name}'",
        "code": _caption_code(target_page_id, container_name, label),
    })

    return calls


def _find_page_js(page_id: str) -> str:
    return f"""let page = null;
for (const p of figma.root.children) {{
  if (p.id === {json.dumps(page_id)}) {{ page = p; break; }}
}}
if (!page) throw new Error('Page {page_id} not found');
await figma.setCurrentPageAsync(page);"""


def _container_code(page_id: str, w: int, h: int, x: int, y: int, name: str) -> str:
    return f"""{_HELPERS_JS}

{_find_page_js(page_id)}

const frame = createContextContainer(page, {w}, {h}, {x}, {y}, {json.dumps(name)});
return frame.id;
"""


def _section_code(page_id: str, container_name: str, sd: SectionData) -> str:
    s = sd.section
    # Embed data as a JS template literal (backtick string).
    # SVG may contain backticks — escape them. PNG base64 never contains backticks.
    if sd.kind == "svg":
        data_js = "`" + sd.data.replace("\\", "\\\\").replace("`", "\\`") + "`"
    else:
        data_js = json.dumps(sd.data)  # plain string, no backtick issues

    return f"""{_HELPERS_JS}

{_find_page_js(page_id)}

const SECTION_DATA = {data_js};
await placeContextSection(page, {json.dumps(container_name)}, {{
  type: {json.dumps(sd.kind)},
  data: SECTION_DATA,
  x: {s.x}, y: {s.y}, w: {s.w}, h: {s.h},
  name: {json.dumps(s.name)},
}});
"""


def _caption_code(page_id: str, container_name: str, label: str) -> str:
    return f"""{_HELPERS_JS}

{_find_page_js(page_id)}

await figma.loadFontAsync({{ family: 'Inter', style: 'Regular' }});
await addContextCaption(page, {json.dumps(container_name)}, {json.dumps(label)});
"""
