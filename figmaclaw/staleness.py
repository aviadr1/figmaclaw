"""Shared helpers for stale frame detection and tombstone matching."""

from __future__ import annotations

from pathlib import Path

from figmaclaw.figma_frontmatter import FigmaPageFrontmatter
from figmaclaw.figma_sync_state import FigmaSyncState


def stale_frame_ids(
    current_hashes: dict[str, str],
    enriched_frame_hashes: dict[str, str] | None,
) -> set[str]:
    """Return frame IDs whose manifest hash differs from enriched hash."""
    enriched = enriched_frame_hashes or {}
    stale: set[str] = set()
    for nid, h in current_hashes.items():
        if nid not in enriched or enriched[nid] != h:
            stale.add(nid)
    for nid in enriched:
        if nid not in current_hashes:
            stale.add(nid)
    return stale


def stale_frame_ids_from_manifest(
    state: FigmaSyncState,
    *,
    file_key: str,
    page_node_id: str,
    enriched_frame_hashes: dict[str, str] | None,
) -> set[str] | None:
    """Return stale frame ids for one page, or None when manifest context is missing."""
    file_entry = state.manifest.files.get(file_key)
    if file_entry is None:
        return None
    page_entry = file_entry.pages.get(page_node_id)
    if page_entry is None:
        return None
    return stale_frame_ids(page_entry.frame_hashes, enriched_frame_hashes)


def active_tombstoned_node_ids(
    fm: FigmaPageFrontmatter | None,
    repo_dir: Path | None,
) -> set[str]:
    """Return node_ids with an ACTIVE tombstone (hash matches manifest).

    An unresolvable-frame tombstone is active when the recorded
    ``frame_hash`` equals the frame's current manifest hash — meaning the
    Figma content has not changed since the LLM gave up on it. When the
    content changes (hash moves), the tombstone auto-invalidates and the
    frame becomes pending again (one retry per content change).

    Returns an empty set when:
    - *fm* has no unresolvable_frames
    - *repo_dir* is None (callers without manifest access)
    - the manifest can't be loaded or doesn't contain the file/page

    Pairing with :func:`stale_frame_ids` is intentional — the two
    functions are mirror queries against the same
    enriched_frame_hashes / unresolvable_frames / manifest_hashes
    triangle. Keep them together so any future contributor touching the
    logic sees both.
    """
    if fm is None or repo_dir is None:
        return set()
    if not fm.unresolvable_frames:
        return set()
    if not fm.file_key or not fm.page_node_id:
        return set()
    try:
        state = FigmaSyncState(repo_dir)
        state.load()
    except Exception:
        return set()
    file_entry = state.manifest.files.get(fm.file_key)
    if file_entry is None:
        return set()
    page_entry = file_entry.pages.get(fm.page_node_id)
    if page_entry is None:
        return set()
    current = page_entry.frame_hashes
    return {
        nid
        for nid, tombstone_hash in fm.unresolvable_frames.items()
        if current.get(nid) == tombstone_hash
    }
