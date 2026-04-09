"""Core incremental pull logic for figmaclaw.

Three-level short-circuit:
1. File-level: compare version + lastModified — skip entire file if unchanged
2. Page-level: compare structural hash — skip page if unchanged
3. Frame-level: preserve existing descriptions for unchanged frames

Output:
- Screen sections → figma/{file-slug}/pages/{page-slug}.md
- Component library sections → figma/{file-slug}/components/{section-slug}.md
  (one .md per section, not per page, so components are individually addressable)

Resumability: manifest is saved after every successfully written page so that
a timeout or crash never causes full re-work on the next run.

Parallelism: page nodes for a single file are fetched concurrently (asyncio.gather)
when no max_pages limit is set. With a limit, pages are fetched sequentially so we
don't waste API calls on pages we'll never process.

on_page_written: optional callback called after each page is written to disk.
Use this to trigger git commits from the caller (keeps git logic out of pull_logic).

DESIGN CONTRACT — body vs frontmatter:
- Frontmatter is machine-readable source of truth. pull_logic reads and writes it.
  Use frontmatter to know WHAT needs updating (which frames changed, new flows, etc).
- Body is human/LLM-readable prose: page summary, section intros, frame tables,
  Mermaid flowcharts. The body is generated and updated by the figma-enrich-page
  skill via LLM — NEVER by code parsing or mechanical rewriting.
- write_new_page() writes a skeleton body (with LLM placeholders) for NEW pages only.
  For existing pages, update_page_frontmatter() updates only the frontmatter, leaving
  the LLM-authored body completely untouched.
- NEVER parse prose from the body in Python code. No parse_page_summary(),
  no parse_section_intros(), no extracting text between headings.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_frontmatter import (
    CURRENT_PULL_SCHEMA_VERSION,
    FrameComposition,
    RawTokenCounts,
    SectionNode,
)
from figmaclaw.figma_hash import compute_frame_hashes, compute_page_hash
from figmaclaw.figma_models import FigmaPage, FigmaSection, from_page_node
from figmaclaw.figma_parse import parse_flows, parse_frontmatter
from figmaclaw.figma_paths import component_path, page_path, slugify
from figmaclaw.figma_render import build_page_frontmatter, render_component_section, scaffold_page
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry
from figmaclaw.figma_utils import write_json_if_changed
from figmaclaw.token_catalog import load_catalog, merge_bindings, save_catalog
from figmaclaw.token_scan import PageTokenScan, scan_page

log = logging.getLogger(__name__)


class PullResult(BaseModel):
    """Summary of a pull_file run."""

    file_key: str
    skipped_file: bool = False
    no_access: bool = False  # True when get_file_meta returns HTTP 400 (restricted file)
    pages_written: int = 0
    pages_skipped: int = 0
    pages_errored: int = 0
    pages_schema_upgraded: int = (
        0  # schema-format-only refresh, hash unchanged, not counted toward max_pages
    )
    md_paths: list[str] = Field(default_factory=list)
    component_sections_written: int = 0
    component_paths: list[str] = Field(default_factory=list)
    has_more: bool = False  # True when max_pages was hit and more pages remain


def write_new_page(
    repo_root: Path,
    page: FigmaPage,
    entry: PageEntry,
    *,
    raw_frames: dict[str, FrameComposition] | None = None,
    raw_tokens: dict[str, RawTokenCounts] | None = None,
    frame_sections: dict[str, list[SectionNode]] | None = None,
) -> Path:
    """Write a NEW scaffold .md for a FigmaPage (screen sections only) and return the path.

    Only call this when the file does NOT exist yet. For existing files, use
    update_page_frontmatter() which preserves the LLM-authored body.

    entry.md_path must not be None — only call this when there are screen sections to write.
    """
    assert entry.md_path is not None, "entry.md_path must be set to call write_new_page()"
    out_path = repo_root / entry.md_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        scaffold_page(
            page, entry, raw_frames=raw_frames, raw_tokens=raw_tokens, frame_sections=frame_sections
        )
    )
    return out_path


def update_page_frontmatter(
    repo_root: Path,
    page: FigmaPage,
    entry: PageEntry,
    *,
    raw_frames: dict[str, FrameComposition] | None = None,
    raw_tokens: dict[str, RawTokenCounts] | None = None,
    frame_sections: dict[str, list[SectionNode]] | None = None,
) -> Path:
    """Update ONLY the frontmatter of an existing page .md file, preserving the body.

    Uses python-frontmatter to cleanly separate frontmatter from body, then rebuilds
    the frontmatter from the FigmaPage model using _build_frontmatter() (which produces
    the correct flow-style YAML formatting).

    entry.md_path must not be None and the file must already exist.

    raw_frames: freshly computed from the pull pass for this page. Replaces any
    existing raw_frames value (pull-pass data is always current). None means the
    field was not computed and is omitted from the output frontmatter.
    frame_sections: freshly computed per-frame section map. Same semantics as raw_frames.
    """
    assert entry.md_path is not None, "entry.md_path must be set"
    out_path = repo_root / entry.md_path
    assert out_path.exists(), f"update_page_frontmatter requires existing file: {out_path}"

    from figmaclaw.figma_parse import split_frontmatter

    md_text = out_path.read_text()
    parts = split_frontmatter(md_text)
    assert parts is not None, f"Failed to parse frontmatter from {out_path}"
    _, body = parts

    # Preserve enrichment state from existing frontmatter (set by enrich pass, not pull pass)
    existing_fm = parse_frontmatter(md_text)
    enriched_hash = existing_fm.enriched_hash if existing_fm else None
    enriched_at = existing_fm.enriched_at if existing_fm else None
    enriched_frame_hashes = existing_fm.enriched_frame_hashes if existing_fm else None

    new_fm = build_page_frontmatter(
        page,
        enriched_hash=enriched_hash,
        enriched_at=enriched_at,
        enriched_frame_hashes=enriched_frame_hashes or None,
        raw_frames=raw_frames,
        raw_tokens=raw_tokens,
        frame_sections=frame_sections,
    )
    out_path.write_text(f"{new_fm}\n{body}")
    return out_path


def write_component_section(
    repo_root: Path,
    section: FigmaSection,
    page: FigmaPage,
    md_rel_path: str,
    *,
    component_set_keys: dict[str, str] | None = None,
) -> Path:
    """Render a single component library section to disk and return the absolute path written."""
    out_path = repo_root / md_rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        render_component_section(section, page, component_set_keys=component_set_keys)
    )
    return out_path


def _aggregate_issues(issues: list) -> list[dict]:
    """Aggregate per-node issues into compact (property, value, classification) histogram.

    Groups issues by their matchable fields (property, classification, hex or
    current_value) and returns one entry per unique combo with a count.
    This reduces file size by ~100x on complex pages while preserving all data
    needed by suggest-tokens.
    """
    from collections import Counter

    # Build a key that captures everything suggest-tokens uses for matching
    buckets: Counter[tuple] = Counter()
    representatives: dict[tuple, dict] = {}

    for issue in issues:
        prop = issue.property
        cls = issue.classification
        hex_val = issue.hex
        cur_val = issue.current_value
        stale_var = issue.stale_variable_id

        # Normalize current_value for grouping: round floats, stringify dicts
        if isinstance(cur_val, float):
            norm_val: object = round(cur_val, 4)
        elif isinstance(cur_val, dict):
            # Color dicts — use hex as the grouping key (already derived)
            norm_val = None  # hex is the key for colors
        else:
            norm_val = cur_val

        key = (prop, cls, hex_val, norm_val, stale_var)
        buckets[key] += 1

        if key not in representatives:
            entry: dict = {"property": prop, "classification": cls}
            if hex_val is not None:
                entry["hex"] = hex_val
            if cur_val is not None:
                entry["current_value"] = cur_val
            if stale_var is not None:
                entry["stale_variable_id"] = stale_var
            representatives[key] = entry

    result = []
    for key, count in buckets.items():
        entry = dict(representatives[key])
        entry["count"] = count
        result.append(entry)

    return result


def _write_token_sidecar(
    screen_md: Path,
    file_key: str,
    page_node_id: str,
    token_scan: PageTokenScan,
) -> None:
    """Write the .tokens.json sidecar file next to the screen .md file.

    Schema v2: issues are aggregated by (property, classification, value)
    with a count field, instead of one entry per node.  This reduces file
    size by ~100x on complex pages while preserving all data needed by
    suggest-tokens.
    """
    sidecar_path = screen_md.with_suffix(".tokens.json")
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    frames_data: dict = {}
    for fid, fscan in token_scan.frames.items():
        frames_data[fid] = {
            "name": fscan.name,
            "summary": {"raw": fscan.raw, "stale": fscan.stale, "valid": fscan.valid},
            "issues": _aggregate_issues(fscan.issues),
        }

    sidecar = {
        "schema_version": 2,
        "file_key": file_key,
        "page_node_id": page_node_id,
        "generated_at": now,
        "summary": {
            "raw": token_scan.raw,
            "stale": token_scan.stale,
            "valid": token_scan.valid,
        },
        "frames": frames_data,
    }

    write_json_if_changed(sidecar_path, sidecar, ignore_keys=frozenset({"generated_at"}))


def _compute_raw_frames(
    frame_docs: dict[str, dict],
) -> tuple[dict[str, FrameComposition], dict[str, list[SectionNode]]]:
    """Classify direct children of each frame node into raw vs DS-component instances,
    and extract per-child section position data.

    frame_docs: {node_id: document_node} as returned by FigmaClient.get_nodes().

    Returns a 2-tuple:
      raw_frames:     sparse dict — only frames with at least one raw child.
                      Absence means fully componentized (signals "clean" to audit skills).
      frame_sections: dense dict — ALL frames, each with their direct children's positions
                      plus direct-child composition inventory (instances/raw_count).
                      Used by build-context + component coverage queries (#35/#38).

    raw:  count of non-INSTANCE direct children (FRAME, GROUP, RECTANGLE, TEXT, etc.)
    ds:   names of INSTANCE children with duplicates — [ButtonV2, ButtonV2] means 2 instances.
    """
    raw_frames: dict[str, FrameComposition] = {}
    frame_sections: dict[str, list[SectionNode]] = {}
    if not isinstance(frame_docs, dict):
        return raw_frames, frame_sections

    def _section_inventory(section_node: dict) -> tuple[list[str], list[str], int]:
        """Return (instance_names, instance_component_ids, raw_count) for one section node."""
        children_raw = section_node.get("children", [])
        if not isinstance(children_raw, list):
            return ([], [], 0)
        direct_children = [c for c in children_raw if isinstance(c, dict)]
        instances: list[str] = []
        instance_component_ids: list[str] = []
        raw_count = 0
        for child in direct_children:
            if child.get("type") == "INSTANCE":
                instances.append(child.get("name", ""))
                component_id = str(child.get("componentId", "")).strip()
                if component_id:
                    instance_component_ids.append(component_id)
            else:
                raw_count += 1
        return (instances, instance_component_ids, raw_count)

    for node_id, node in frame_docs.items():
        if not isinstance(node, dict):
            continue
        children_raw = node.get("children", [])
        if not isinstance(children_raw, list):
            children_raw = []
        children: list[dict] = [c for c in children_raw if isinstance(c, dict)]
        frame_bb = node.get("absoluteBoundingBox", {})
        frame_x: float = frame_bb.get("x", 0)
        frame_y: float = frame_bb.get("y", 0)

        raw_count = 0
        ds_names: list[str] = []
        sections: list[SectionNode] = []

        for child in children:
            child_bb = child.get("absoluteBoundingBox", {})
            instances, component_ids, section_raw_count = _section_inventory(child)
            sections.append(
                SectionNode(
                    node_id=child.get("id", ""),
                    name=child.get("name", ""),
                    x=round(child_bb.get("x", 0) - frame_x),
                    y=round(child_bb.get("y", 0) - frame_y),
                    w=round(child_bb.get("width", 0)),
                    h=round(child_bb.get("height", 0)),
                    instances=instances,
                    instance_component_ids=component_ids,
                    raw_count=section_raw_count,
                )
            )
            if child.get("type") == "INSTANCE":
                ds_names.append(child.get("name", ""))
            else:
                raw_count += 1

        if raw_count > 0:
            raw_frames[node_id] = FrameComposition(raw=raw_count, ds=ds_names)
        if sections:
            frame_sections[node_id] = sections

    return raw_frames, frame_sections


def _build_component_set_keys(
    page_node_id: str,
    component_sets: list[dict],
) -> dict[str, str]:
    """Build the component_set_keys dict for all component sections on a given page.

    The Figma /component_sets endpoint returns published COMPONENT_SET nodes.
    Each has a containing_frame.pageId that identifies which page it lives on.
    Matching by pageId (not by section frame node IDs) is necessary because:
    - Published component sets are direct page-level children, not inside sections.
    - Private/locked base-component sets inside sections are NOT returned by the API.
    - All published sets on a page are relevant to any section on that page.

    Returns {component_set_name: figma_key} for use with importComponentSetByKeyAsync().
    """
    return {
        cs["name"]: cs["key"]
        for cs in component_sets
        if cs.get("containing_frame", {}).get("pageId") == page_node_id
        and cs.get("key")
        and cs.get("name")
    }


def _merge_existing(page: FigmaPage, existing_flows: list[tuple[str, str]]) -> FigmaPage:
    """Return a copy of the page with flows restored from existing .md files.

    existing_flows: [(src, dst), ...] — from the screen page .md only

    Descriptions are NOT merged — they live in the body, not frontmatter.
    The body is preserved byte-for-byte by update_page_frontmatter().
    """
    existing_flow_set = set(page.flows)
    merged_flows = list(page.flows)
    for edge in existing_flows:
        if edge not in existing_flow_set:
            merged_flows.append(edge)
            existing_flow_set.add(edge)

    return page.model_copy(update={"flows": merged_flows})


async def pull_file(
    client: FigmaClient,
    file_key: str,
    state: FigmaSyncState,
    repo_root: Path,
    *,
    force: bool = False,
    max_pages: int | None = None,
    progress: Callable[[str], None] | None = None,
    on_page_written: Callable[[str, list[str]], None] | None = None,
) -> PullResult:
    """Pull all (or up to max_pages) changed pages for a tracked Figma file.

    Screen sections → figma/{file-slug}/pages/{page-slug}.md
    Component library sections → figma/{file-slug}/components/{section-slug}.md

    Manifest is saved after each page so a crash/timeout/quota error mid-run
    doesn't cause re-work — a subsequent call picks up from where this left off.

    max_pages: stop after writing this many Figma pages (pages whose hash changed).
               Skipped pages don't count. Set result.has_more=True if more remain.

    on_page_written: called after each page is successfully written to disk with
               (page_name, [paths_written]). Use this to trigger git commits from
               the caller without coupling pull_logic to git.

    progress:  optional callback called with a human-readable status line for
               each page as it is processed.

    Returns a PullResult describing what was done.
    """

    def _progress(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    result = PullResult(file_key=file_key)
    catalog = load_catalog(repo_root)

    # Level 1: file-level version check
    try:
        meta = await client.get_file_meta(file_key)
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 400:
            log.warning("No access to %r (HTTP 400) — will be moved to skipped_files", file_key)
            result.skipped_file = True
            result.no_access = True
        else:
            log.error("Failed to fetch file meta for %r: %s — skipping file", file_key, exc)
            result.skipped_file = True
        return result
    api_version = meta.version
    api_last_modified = meta.lastModified
    file_name = meta.name

    stored = state.manifest.files.get(file_key)
    schema_stale = (stored.pull_schema_version if stored else 0) < CURRENT_PULL_SCHEMA_VERSION
    if (
        not force
        and not schema_stale
        and stored
        and stored.version == api_version
        and stored.last_modified == api_last_modified
    ):
        _progress(f"{file_name}: unchanged (version {api_version}), skipping all pages")
        result.skipped_file = True
        return result
    if schema_stale and stored and stored.version == api_version:
        _progress(
            f"{file_name}: pull schema stale (v{stored.pull_schema_version} → v{CURRENT_PULL_SCHEMA_VERSION}), refreshing frontmatter"
        )

    now = datetime.datetime.now(datetime.UTC).isoformat()
    state.set_file_meta(
        file_key,
        version=api_version,
        last_modified=api_last_modified,
        last_checked_at=now,
    )

    # Fetch component sets once per changed file. Used to populate component_set_keys
    # in component section .md frontmatter so build skills can skip search_design_system().
    try:
        component_sets = await client.get_component_sets(file_key)
    except Exception as exc:
        log.warning(
            "Failed to fetch component sets for %r: %s — component_set_keys will be empty",
            file_key,
            exc,
        )
        component_sets = []

    page_stubs = meta.canvas_pages
    file_slug = slugify(file_name, fallback=file_key)
    total_pages = len(page_stubs)
    pages_written_this_call = 0

    # Fetch all page nodes concurrently when there is no page limit (fastest path).
    # With a page limit, fetch sequentially to avoid wasting API calls on pages we
    # will never process in this batch.
    # skip_pages stubs are excluded from the parallel fetch to avoid wasted API calls.
    if max_pages is None:
        fetch_stubs = [s for s in page_stubs if not state.should_skip_page(s.name)]

        async def _fetch(node_id: str) -> dict | Exception:
            try:
                return await client.get_page(file_key, node_id)
            except Exception as exc:
                return exc

        fetched = await asyncio.gather(*[_fetch(s.id) for s in fetch_stubs])
        page_nodes_map: dict[str, dict | Exception] = {
            stub.id: result for stub, result in zip(fetch_stubs, fetched, strict=False)
        }
    else:
        page_nodes_map = {}  # populated lazily below

    # Batch-fetch direct children of ALL screen frames across ALL pages in one get_nodes call.
    # This is O(1) per file instead of O(pages), which makes schema-stale backfills fast.
    # Only done in parallel mode (max_pages=None) where we already have all page nodes.
    # In sequential mode, fall back to per-page get_nodes inside the loop.
    all_frame_docs: dict[str, dict] = {}
    if max_pages is None:
        all_screen_frame_ids: list[str] = []
        for stub_id, pn in page_nodes_map.items():
            if isinstance(pn, Exception):
                continue
            new_hash = compute_page_hash(pn)
            stored_hash = state.get_page_hash(file_key, stub_id)
            if not force and stored_hash == new_hash and not schema_stale:
                # When schema is stale we still need frame children for unchanged pages
                # so newly introduced frontmatter fields (e.g. frame_sections) can be
                # backfilled in one schema-upgrade pass.
                continue
            for section in pn.get("children", []):
                if section.get("type") == "SECTION":
                    for child in section.get("children", []):
                        if child.get("type") == "FRAME":
                            all_screen_frame_ids.append(child["id"])
        if all_screen_frame_ids:
            try:
                # Chunk to avoid 414 URI Too Large (Figma GET limit ~200 IDs per call).
                chunk_size = 200
                for i in range(0, len(all_screen_frame_ids), chunk_size):
                    chunk = all_screen_frame_ids[i : i + chunk_size]
                    chunk_docs = await client.get_nodes(file_key, chunk, depth=2)
                    if not isinstance(chunk_docs, dict):
                        log.warning(
                            "get_nodes returned non-dict for chunk %d-%d (got %s)",
                            i,
                            i + len(chunk),
                            type(chunk_docs).__name__,
                        )
                        continue
                    all_frame_docs.update(chunk_docs)
                log.debug(
                    "Batch-fetched %d frame nodes for file %r", len(all_screen_frame_ids), file_key
                )
            except Exception as exc:
                log.warning(
                    "Failed to batch-fetch frame children (%s) — raw_frames will be omitted",
                    type(exc).__name__,
                )

    for page_idx, page_stub in enumerate(page_stubs, 1):
        page_node_id: str = page_stub.id
        page_name: str = page_stub.name

        if state.should_skip_page(page_name):
            _progress(
                f"  [{page_idx}/{total_pages}] {page_name} — skipped (matches skip_pages pattern)"
            )
            result.pages_skipped += 1
            continue

        # Level 2: structural hash check
        if max_pages is None:
            # Already fetched above
            page_node_or_exc = page_nodes_map[page_node_id]
            if isinstance(page_node_or_exc, Exception):
                log.error(
                    "Failed to fetch page %r (%s): %s — skipping",
                    page_name,
                    page_node_id,
                    page_node_or_exc,
                )
                result.pages_errored += 1
                continue
            page_node: dict = page_node_or_exc
        else:
            # Sequential fetch.
            # When schema is current: stop before fetching if content-change budget is hit.
            # When schema is stale: always fetch so we can upgrade frontmatter format.
            # Schema-only upgrades (hash unchanged) don't consume the budget, so they
            # can't cause has_more=True and won't block pull_schema_version from updating.
            if not schema_stale and pages_written_this_call >= max_pages:
                result.has_more = True
                _progress(
                    f"  [{page_idx}/{total_pages}] {page_name} — reached max_pages={max_pages}, stopping"
                )
                break
            try:
                page_node = await client.get_page(file_key, page_node_id)
            except Exception as exc:
                log.error(
                    "Failed to fetch page %r (%s): %s — skipping", page_name, page_node_id, exc
                )
                result.pages_errored += 1
                continue

        new_hash = compute_page_hash(page_node)
        stored_hash = state.get_page_hash(file_key, page_node_id)

        content_unchanged = not force and stored_hash is not None and stored_hash == new_hash

        # Schema-only: content is identical but the pull schema version needs refreshing.
        # Frontmatter gets re-rendered to pick up new fields; no token scan is run.
        # These don't consume the max_pages budget so the whole file upgrades in one pass.
        schema_only = content_unchanged and schema_stale

        if content_unchanged and not schema_stale:
            _progress(f"  [{page_idx}/{total_pages}] {page_name} — unchanged (skip)")
            result.pages_skipped += 1
            continue

        # Content-changed pages in sequential mode: enforce budget here.
        # (When schema_stale the early stop above was skipped; we check again here.)
        if not schema_only and max_pages is not None and pages_written_this_call >= max_pages:
            result.has_more = True
            _progress(
                f"  [{page_idx}/{total_pages}] {page_name} — reached max_pages={max_pages}, stopping"
            )
            break

        if schema_only:
            _progress(
                f"  [{page_idx}/{total_pages}] {page_name} — schema upgrade (content unchanged)..."
            )
        else:
            _progress(f"  [{page_idx}/{total_pages}] {page_name} — processing...")

        try:
            node_suffix = page_node_id.replace(":", "-")
            page_slug = f"{slugify(page_name)}-{node_suffix}"
            page = from_page_node(page_node, file_key=file_key, file_name=file_name)
            page = page.model_copy(
                update={
                    "page_slug": page_slug,
                    "version": api_version,
                    "last_modified": api_last_modified,
                }
            )

            # Merge flows from existing .md (descriptions live in body, not frontmatter)
            existing_flows: list[tuple[str, str]] = []

            screen_md_rel = page_path(file_slug, page_slug)
            screen_md = repo_root / screen_md_rel
            if screen_md.exists():
                md_text = screen_md.read_text()
                existing_flows = parse_flows(md_text)

            page = _merge_existing(page, existing_flows)

            # Compute per-frame content hashes for surgical enrichment
            frame_hashes = compute_frame_hashes(page_node)

            screen_sections = [s for s in page.sections if not s.is_component_library]
            component_sections = [s for s in page.sections if s.is_component_library]

            # Compute raw_frames + frame_sections from pre-fetched batch (parallel mode)
            # or per-page call (sequential).
            raw_frames: dict[str, FrameComposition] | None = None
            frame_sections: dict[str, list[SectionNode]] | None = None
            screen_frame_ids = [f.node_id for s in screen_sections for f in s.frames]
            if screen_frame_ids:
                if max_pages is None:
                    # Use the file-level batch already fetched above — O(1) lookup, no extra API call.
                    try:
                        page_frame_docs = {
                            k: v for k, v in all_frame_docs.items() if k in set(screen_frame_ids)
                        }
                        if page_frame_docs:
                            raw_frames, frame_sections = _compute_raw_frames(page_frame_docs)
                    except Exception as exc:
                        log.warning(
                            "Failed to compute raw_frames for page %r (%s): %s — raw_frames will be omitted",
                            page_name,
                            page_node_id,
                            exc,
                        )
                else:
                    # Sequential mode: must fetch per-page (we don't have all page nodes upfront).
                    try:
                        frame_docs = await client.get_nodes(file_key, screen_frame_ids, depth=2)
                        raw_frames, frame_sections = _compute_raw_frames(frame_docs)
                    except Exception as exc:
                        log.warning(
                            "Failed to fetch frame children for page %r (%s): %s — raw_frames will be omitted",
                            page_name,
                            page_node_id,
                            exc,
                        )

            # Scan raw/stale token bindings — zero extra API calls, walks page_node already in memory.
            # Skipped for schema-only upgrades: content is unchanged so token data can't have changed.
            token_scan: PageTokenScan | None = None
            raw_tokens: dict[str, RawTokenCounts] | None = None
            if screen_frame_ids and not schema_only:
                try:
                    token_scan = scan_page(page_node, set(screen_frame_ids))
                    # Sparse frontmatter summary — only frames with at least one issue
                    raw_tokens = {
                        fid: RawTokenCounts(raw=fscan.raw, stale=fscan.stale, valid=fscan.valid)
                        for fid, fscan in token_scan.frames.items()
                        if fscan.raw > 0 or fscan.stale > 0
                    }
                    if token_scan.valid_bindings:
                        merge_bindings(catalog, token_scan.valid_bindings)
                        save_catalog(catalog, repo_root)
                except Exception as exc:
                    log.warning(
                        "Failed to scan tokens for page %r (%s): %s — raw_tokens will be omitted",
                        page_name,
                        page_node_id,
                        exc,
                    )

            written_screen_rel: str | None = None
            if screen_sections:
                screen_page = page.model_copy(update={"sections": screen_sections})
                screen_entry = PageEntry(
                    page_name=page_name,
                    page_slug=page_slug,
                    md_path=screen_md_rel,
                    page_hash=new_hash,
                    last_refreshed_at=now,
                    frame_hashes=frame_hashes,
                )
                if screen_md.exists():
                    written = update_page_frontmatter(
                        repo_root,
                        screen_page,
                        screen_entry,
                        raw_frames=raw_frames,
                        raw_tokens=raw_tokens,
                        frame_sections=frame_sections,
                    )
                else:
                    written = write_new_page(
                        repo_root,
                        screen_page,
                        screen_entry,
                        raw_frames=raw_frames,
                        raw_tokens=raw_tokens,
                        frame_sections=frame_sections,
                    )
                if token_scan is not None:
                    try:
                        _write_token_sidecar(written, page.file_key, page_node_id, token_scan)
                    except Exception as exc:
                        log.warning(
                            "Failed to write token sidecar for page %r: %s",
                            page_name,
                            exc,
                        )
                written_screen_rel = str(written.relative_to(repo_root))
                result.md_paths.append(written_screen_rel)
                if schema_only:
                    result.pages_schema_upgraded += 1
                else:
                    result.pages_written += 1

            written_component_rels: list[str] = []
            for section in component_sections:
                if not section.frames:
                    continue
                sect_suffix = section.node_id.replace(":", "-")
                sect_slug = f"{slugify(section.name)}-{sect_suffix}"
                comp_rel = component_path(file_slug, sect_slug)
                sect_keys = _build_component_set_keys(page.page_node_id, component_sets)
                written = write_component_section(
                    repo_root,
                    section,
                    page,
                    comp_rel,
                    component_set_keys=sect_keys or None,
                )
                written_component_rels.append(str(written.relative_to(repo_root)))

            if written_component_rels:
                result.component_paths.extend(written_component_rels)
                result.component_sections_written += len(written_component_rels)

            n_comps = len(written_component_rels)
            suffix = f" + {n_comps} component(s)" if n_comps else ""
            verb = "schema-upgraded" if schema_only else "wrote"
            _progress(f"  [{page_idx}/{total_pages}] {page_name} — {verb}{suffix}")

        except Exception as exc:
            log.error("Error processing page %r (%s): %s — skipping", page_name, page_node_id, exc)
            result.pages_errored += 1
            continue

        # Save manifest entry
        entry = PageEntry(
            page_name=page_name,
            page_slug=page_slug,
            md_path=written_screen_rel,
            page_hash=new_hash,
            last_refreshed_at=now,
            component_md_paths=written_component_rels,
            frame_hashes=frame_hashes,
        )
        state.set_page_entry(file_key, page_node_id, entry)
        state.save()
        if not schema_only:
            pages_written_this_call += 1

        # Notify caller so it can commit/push incrementally
        if on_page_written:
            all_written = (
                [written_screen_rel] if written_screen_rel else []
            ) + written_component_rels
            on_page_written(f"{file_name} / {page_name}", all_written)

    # Record that all pages in this file are now at the current pull schema version.
    # Only written after the full page loop completes — if interrupted mid-file,
    # the version stays at 0 and the next run re-processes the whole file.
    if not result.has_more and file_key in state.manifest.files:
        state.manifest.files[file_key].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION
        state.save()

    # Structural invariant: schema-only upgrades must never *by themselves* cause has_more=True.
    # If has_more=True while zero content-changed pages consumed the budget in this call
    # (pages_written_this_call == 0) and at least one schema-only upgrade happened, then a
    # schema-only path incorrectly triggered pagination cutoff — that can cause CI loops.
    #
    # Use pages_written_this_call (budget counter), not result.pages_written, because
    # component-only pages increment budget without incrementing pages_written.
    assert not (
        result.has_more and pages_written_this_call == 0 and result.pages_schema_upgraded > 0
    ), (
        "BUG: has_more=True with no budget consumption from content-changed pages "
        f"(pages_written_this_call=0), pages_schema_upgraded={result.pages_schema_upgraded} — "
        "schema-only upgrades must not consume the max_pages budget (causes infinite CI loop)"
    )

    return result
