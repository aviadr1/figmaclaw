"""Shared helpers for stale frame detection across commands."""

from __future__ import annotations

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
