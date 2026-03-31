"""Core incremental pull logic for figmaclaw.

Three-level short-circuit:
1. File-level: compare version + lastModified — skip entire file if unchanged
2. Page-level: compare structural hash — skip page if unchanged
3. Frame-level: preserve existing descriptions for unchanged frames (LLM idempotency)

Output:
- Screen sections → figma/{file-slug}/pages/{page-slug}.md
- Component library sections → figma/{file-slug}/components/{section-slug}.md
  (one .md per section, not per page, so components are individually addressable)

Resumability: manifest is saved after every successfully written page so that
a timeout, crash, or LLM quota error never causes full re-work on the next run.

Parallelism: page nodes for a single file are fetched concurrently (asyncio.gather)
when no max_pages limit is set. With a limit, pages are fetched sequentially so we
don't waste API calls on pages we'll never process.

on_page_written: optional callback called after each page is written to disk.
Use this to trigger git commits from the caller (keeps git logic out of pull_logic).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_hash import compute_page_hash
from figmaclaw.figma_llm import enrich_page_with_descriptions
from figmaclaw.figma_models import FigmaPage, FigmaSection, from_page_node
from figmaclaw.figma_parse import parse_flows, parse_frame_descriptions
from figmaclaw.figma_paths import component_path, page_path, slugify
from figmaclaw.figma_render import render_component_section, render_page
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry

log = logging.getLogger(__name__)


class PullResult(BaseModel):
    """Summary of a pull_file run."""

    file_key: str
    skipped_file: bool = False
    pages_written: int = 0
    pages_skipped: int = 0
    pages_errored: int = 0
    llm_errors: int = 0
    md_paths: list[str] = Field(default_factory=list)
    component_sections_written: int = 0
    component_paths: list[str] = Field(default_factory=list)
    has_more: bool = False  # True when max_pages was hit and more pages remain


def write_page(repo_root: Path, page: FigmaPage, entry: PageEntry) -> Path:
    """Render a FigmaPage (screen sections only) to disk and return the absolute path written.

    entry.md_path must not be None — only call this when there are screen sections to write.
    """
    assert entry.md_path is not None, "entry.md_path must be set to call write_page()"
    out_path = repo_root / entry.md_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_page(page, entry))
    return out_path


def write_component_section(
    repo_root: Path,
    section: FigmaSection,
    page: FigmaPage,
    page_hash: str,
    md_rel_path: str,
) -> Path:
    """Render a single component library section to disk and return the absolute path written."""
    out_path = repo_root / md_rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_component_section(section, page, page_hash))
    return out_path


def _merge_existing(page: FigmaPage, existing_descs: dict[str, str], existing_flows: list[tuple[str, str]]) -> FigmaPage:
    """Return a copy of the page with descriptions and flows restored from existing .md files.

    existing_descs: {node_id: description} — merged from screen page + all component section .mds
    existing_flows: [(src, dst), ...] — from the screen page .md only
    """
    new_sections = []
    for section in page.sections:
        new_frames = []
        for frame in section.frames:
            desc = frame.description or existing_descs.get(frame.node_id, "")
            new_frames.append(frame.model_copy(update={"description": desc}))
        new_sections.append(section.model_copy(update={"frames": new_frames}))

    existing_flow_set = set(page.flows)
    merged_flows = list(page.flows)
    for edge in existing_flows:
        if edge not in existing_flow_set:
            merged_flows.append(edge)
            existing_flow_set.add(edge)

    return page.model_copy(update={"sections": new_sections, "flows": merged_flows})


async def _enrich_safe(anthropic_client: object, page: FigmaPage, result: PullResult) -> FigmaPage:
    """Call LLM enrichment, catching quota/API errors gracefully."""
    try:
        enriched = await enrich_page_with_descriptions(anthropic_client, page)  # type: ignore[arg-type]
        return enriched
    except Exception as exc:
        log.warning("LLM enrichment failed for page %r: %s — writing with placeholders", page.page_name, exc)
        result.llm_errors += 1
        return page


async def pull_file(
    client: FigmaClient,
    file_key: str,
    state: FigmaSyncState,
    repo_root: Path,
    *,
    force: bool = False,
    anthropic_client: object | None = None,
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
    api_version = meta.get("version", "")
    api_last_modified = meta.get("lastModified", "")
    file_name = meta.get("name", file_key)

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

    doc = meta.get("document", {})
    page_stubs = [c for c in doc.get("children", []) if c.get("type") == "CANVAS"]
    file_slug = slugify(file_name, fallback=file_key)
    total_pages = len(page_stubs)
    pages_written_this_call = 0

    # Fetch all page nodes concurrently when there is no page limit (fastest path).
    # With a page limit, fetch sequentially to avoid wasting API calls on pages we
    # will never process in this batch.
    if max_pages is None:
        async def _fetch(stub: dict) -> dict | Exception:
            try:
                return await client.get_page(file_key, stub["id"])
            except Exception as exc:
                return exc

        fetched = await asyncio.gather(*[_fetch(stub) for stub in page_stubs])
        page_nodes_iter: list[dict | Exception] = list(fetched)
    else:
        page_nodes_iter = []  # populated lazily below

    for page_idx, page_stub in enumerate(page_stubs, 1):
        page_node_id: str = page_stub["id"]
        page_name: str = page_stub.get("name", "")

        # Level 2: structural hash check
        if max_pages is None:
            # Already fetched above
            page_node_or_exc = page_nodes_iter[page_idx - 1]
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

            # Level 3: preserve existing descriptions from all existing .mds before LLM runs
            all_existing_descs: dict[str, str] = {}
            existing_flows: list[tuple[str, str]] = []

            screen_md_rel = page_path(file_slug, page_slug)
            screen_md = repo_root / screen_md_rel
            if screen_md.exists():
                md_text = screen_md.read_text()
                all_existing_descs.update(parse_frame_descriptions(md_text))
                existing_flows = parse_flows(md_text)

            for section in page.sections:
                if section.is_component_library:
                    sect_suffix = section.node_id.replace(":", "-")
                    sect_slug = f"{slugify(section.name)}-{sect_suffix}"
                    comp_md = repo_root / component_path(file_slug, sect_slug)
                    if comp_md.exists():
                        all_existing_descs.update(parse_frame_descriptions(comp_md.read_text()))

            page = _merge_existing(page, all_existing_descs, existing_flows)

            if anthropic_client is not None:
                page = await _enrich_safe(anthropic_client, page, result)

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
                )
                written = write_page(repo_root, screen_page, screen_entry)
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
                written = write_component_section(repo_root, section, page, new_hash, comp_rel)
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
        )
        state.set_page_entry(file_key, page_node_id, entry)
        state.save()
        pages_written_this_call += 1

        # Notify caller so it can commit/push incrementally
        if on_page_written:
            all_written = ([written_screen_rel] if written_screen_rel else []) + written_component_rels
            on_page_written(f"{file_name} / {page_name}", all_written)

    return result
