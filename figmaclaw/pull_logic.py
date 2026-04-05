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

from pydantic import BaseModel, Field

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_hash import compute_frame_hashes, compute_page_hash
from figmaclaw.figma_models import FigmaPage, FigmaSection, from_page_node
from figmaclaw.figma_parse import parse_flows, parse_frontmatter
from figmaclaw.figma_paths import component_path, page_path, slugify
from figmaclaw.figma_render import build_page_frontmatter, render_component_section, scaffold_page
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry

log = logging.getLogger(__name__)


class PullResult(BaseModel):
    """Summary of a pull_file run."""

    file_key: str
    skipped_file: bool = False
    pages_written: int = 0
    pages_skipped: int = 0
    pages_errored: int = 0
    md_paths: list[str] = Field(default_factory=list)
    component_sections_written: int = 0
    component_paths: list[str] = Field(default_factory=list)
    has_more: bool = False  # True when max_pages was hit and more pages remain


def write_new_page(repo_root: Path, page: FigmaPage, entry: PageEntry) -> Path:
    """Write a NEW scaffold .md for a FigmaPage (screen sections only) and return the path.

    Only call this when the file does NOT exist yet. For existing files, use
    update_page_frontmatter() which preserves the LLM-authored body.

    entry.md_path must not be None — only call this when there are screen sections to write.
    """
    assert entry.md_path is not None, "entry.md_path must be set to call write_new_page()"
    out_path = repo_root / entry.md_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(scaffold_page(page, entry))
    return out_path


def update_page_frontmatter(repo_root: Path, page: FigmaPage, entry: PageEntry) -> Path:
    """Update ONLY the frontmatter of an existing page .md file, preserving the body.

    Uses python-frontmatter to cleanly separate frontmatter from body, then rebuilds
    the frontmatter from the FigmaPage model using _build_frontmatter() (which produces
    the correct flow-style YAML formatting).

    entry.md_path must not be None and the file must already exist.
    """
    assert entry.md_path is not None, "entry.md_path must be set"
    out_path = repo_root / entry.md_path
    assert out_path.exists(), f"update_page_frontmatter requires existing file: {out_path}"

    from figmaclaw.figma_parse import split_frontmatter

    md_text = out_path.read_text()
    parts = split_frontmatter(md_text)
    assert parts is not None, f"Failed to parse frontmatter from {out_path}"
    _, body = parts

    # Preserve enrichment state from existing frontmatter
    existing_fm = parse_frontmatter(md_text)
    enriched_hash = existing_fm.enriched_hash if existing_fm else None
    enriched_at = existing_fm.enriched_at if existing_fm else None
    enriched_frame_hashes = existing_fm.enriched_frame_hashes if existing_fm else None

    new_fm = build_page_frontmatter(
        page,
        enriched_hash=enriched_hash,
        enriched_at=enriched_at,
        enriched_frame_hashes=enriched_frame_hashes or None,
    )
    out_path.write_text(f"{new_fm}\n{body}")
    return out_path


def write_component_section(
    repo_root: Path,
    section: FigmaSection,
    page: FigmaPage,
    md_rel_path: str,
) -> Path:
    """Render a single component library section to disk and return the absolute path written."""
    out_path = repo_root / md_rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_component_section(section, page))
    return out_path


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

    # Level 1: file-level version check
    try:
        meta = await client.get_file_meta(file_key)
    except Exception as exc:
        log.error("Failed to fetch file meta for %r: %s — skipping file", file_key, exc)
        result.skipped_file = True
        return result
    api_version = meta.version
    api_last_modified = meta.lastModified
    file_name = meta.name

    stored = state.manifest.files.get(file_key)
    if not force and stored and stored.version == api_version and stored.last_modified == api_last_modified:
        _progress(f"{file_name}: unchanged (version {api_version}), skipping all pages")
        result.skipped_file = True
        return result

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    state.set_file_meta(
        file_key,
        version=api_version,
        last_modified=api_last_modified,
        last_checked_at=now,
    )

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
            stub.id: result for stub, result in zip(fetch_stubs, fetched)
        }
    else:
        page_nodes_map = {}  # populated lazily below

    for page_idx, page_stub in enumerate(page_stubs, 1):
        page_node_id: str = page_stub.id
        page_name: str = page_stub.name

        if state.should_skip_page(page_name):
            _progress(f"  [{page_idx}/{total_pages}] {page_name} — skipped (matches skip_pages pattern)")
            result.pages_skipped += 1
            continue

        # Level 2: structural hash check
        if max_pages is None:
            # Already fetched above
            page_node_or_exc = page_nodes_map[page_node_id]
            if isinstance(page_node_or_exc, Exception):
                log.error("Failed to fetch page %r (%s): %s — skipping", page_name, page_node_id, page_node_or_exc)
                result.pages_errored += 1
                continue
            page_node: dict = page_node_or_exc
        else:
            # Sequential fetch — stop early if budget already hit
            if pages_written_this_call >= max_pages:
                result.has_more = True
                _progress(f"  [{page_idx}/{total_pages}] {page_name} — reached max_pages={max_pages}, stopping")
                break
            try:
                page_node = await client.get_page(file_key, page_node_id)
            except Exception as exc:
                log.error("Failed to fetch page %r (%s): %s — skipping", page_name, page_node_id, exc)
                result.pages_errored += 1
                continue

        new_hash = compute_page_hash(page_node)
        stored_hash = state.get_page_hash(file_key, page_node_id)

        if not force and stored_hash == new_hash:
            _progress(f"  [{page_idx}/{total_pages}] {page_name} — unchanged (skip)")
            result.pages_skipped += 1
            continue

        _progress(f"  [{page_idx}/{total_pages}] {page_name} — processing...")

        try:
            node_suffix = page_node_id.replace(":", "-")
            page_slug = f"{slugify(page_name)}-{node_suffix}"
            page = from_page_node(page_node, file_key=file_key, file_name=file_name)
            page = page.model_copy(update={"page_slug": page_slug, "version": api_version, "last_modified": api_last_modified})

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
                    written = update_page_frontmatter(repo_root, screen_page, screen_entry)
                else:
                    written = write_new_page(repo_root, screen_page, screen_entry)
                written_screen_rel = str(written.relative_to(repo_root))
                result.md_paths.append(written_screen_rel)
                result.pages_written += 1

            written_component_rels: list[str] = []
            for section in component_sections:
                if not section.frames:
                    continue
                sect_suffix = section.node_id.replace(":", "-")
                sect_slug = f"{slugify(section.name)}-{sect_suffix}"
                comp_rel = component_path(file_slug, sect_slug)
                written = write_component_section(repo_root, section, page, comp_rel)
                written_component_rels.append(str(written.relative_to(repo_root)))

            if written_component_rels:
                result.component_paths.extend(written_component_rels)
                result.component_sections_written += len(written_component_rels)

            n_comps = len(written_component_rels)
            suffix = f" + {n_comps} component(s)" if n_comps else ""
            _progress(f"  [{page_idx}/{total_pages}] {page_name} — wrote{suffix}")

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
        pages_written_this_call += 1

        # Notify caller so it can commit/push incrementally
        if on_page_written:
            all_written = ([written_screen_rel] if written_screen_rel else []) + written_component_rels
            on_page_written(f"{file_name} / {page_name}", all_written)

    return result
