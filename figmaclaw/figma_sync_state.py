"""Manages .figma-sync/manifest.json — the persistent state for figmaclaw.

The manifest tracks which Figma files are synced, their current API versions,
and per-page structural hashes. It is committed to the repo alongside the
generated .md files — never .gitignored.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from figmaclaw.figma_utils import write_json_if_changed

MANIFEST_TIMESTAMP_KEYS = frozenset({"last_checked_at", "last_refreshed_at"})


class PageEntry(BaseModel):
    """State for a single Figma page within a tracked file.

    md_path is None when the page has no screen sections (all sections are
    component libraries — only component_md_paths are written in that case).
    """

    page_name: str
    page_slug: str
    md_path: str | None = None  # None for all-component pages
    page_hash: str
    last_refreshed_at: str
    component_md_paths: list[str] = Field(default_factory=list)
    frame_hashes: dict[str, str] = Field(default_factory=dict)  # {node_id: content_hash}


class FileEntry(BaseModel):
    """State for a tracked Figma file."""

    file_name: str
    version: str
    last_modified: str
    last_checked_at: str = ""
    source_project_id: str | None = None
    source_project_name: str | None = None
    source_lifecycle: str = "unknown"
    library_hash: str | None = None
    # 0 = pre-versioning. Set to CURRENT_PULL_SCHEMA_VERSION after a successful pull
    # that wrote all pages at the current schema. Files below CURRENT_PULL_SCHEMA_VERSION
    # get frontmatter re-written on next pull even if Figma content is unchanged.
    pull_schema_version: int = 0
    pages: dict[str, PageEntry] = Field(default_factory=dict)


class Manifest(BaseModel):
    """Root manifest model persisted to .figma-sync/manifest.json.

    Skip filters
    ------------
    Add a file key to ``skipped_files`` to permanently exclude it from every
    pull run without removing it from ``tracked_files``.  The value is a
    human-readable reason string shown in ``figmaclaw pull`` output.

    Example::

        "skipped_files": {
            "ueCg0J6cIauP2sxZULxU7F": "no access — returns 400 on get_file_meta",
            "WXhYDTovwb5vCcjisWwq0j": "scratch file — not useful to sync"
        }

    The auto-discovery step (``--team-id``) will never add a file to
    ``tracked_files`` if it is already present in ``skipped_files``.
    """

    schema_version: int = 1
    skip_pages: list[str] = Field(
        default_factory=lambda: ["old-*", "old *", "---"],
        description=(
            "Glob patterns (case-insensitive) for page names to skip during sync. "
            "Edit this list in .figma-sync/manifest.json to customise. "
            "Examples: 'old-*', '📦*', '---', 'archive*'"
        ),
    )
    tracked_files: list[str] = Field(default_factory=list)
    skipped_files: dict[str, str] = Field(default_factory=dict)
    files: dict[str, FileEntry] = Field(default_factory=dict)


class FigmaSyncState:
    """Manages .figma-sync/manifest.json for a repo."""

    def __init__(self, repo_root: Path | str) -> None:
        self._sync_dir = Path(repo_root) / ".figma-sync"
        self._manifest_file = self._sync_dir / "manifest.json"
        self.manifest = Manifest()

    def load(self) -> None:
        """Load manifest from disk. No-op if file doesn't exist."""
        if self._manifest_file.exists():
            self.manifest = Manifest.model_validate_json(self._manifest_file.read_text())

    def save(self) -> None:
        """Persist manifest to disk."""
        self._sync_dir.mkdir(parents=True, exist_ok=True)
        write_json_if_changed(
            self._manifest_file,
            self.manifest.model_dump(mode="json"),
            ignore_keys=MANIFEST_TIMESTAMP_KEYS,
        )

    def get_page_hash(self, file_key: str, page_node_id: str) -> str | None:
        """Return the stored hash for a page, or None if unknown."""
        file_entry = self.manifest.files.get(file_key)
        if file_entry is None:
            return None
        page_entry = file_entry.pages.get(page_node_id)
        if page_entry is None:
            return None
        return page_entry.page_hash

    def get_file_version(self, file_key: str) -> str | None:
        """Return the stored version for a file, or None if unknown."""
        file_entry = self.manifest.files.get(file_key)
        return file_entry.version if file_entry else None

    def add_tracked_file(self, file_key: str, file_name: str) -> None:
        """Add a file to the tracked list. Idempotent — no duplicates."""
        if file_key not in self.manifest.tracked_files:
            self.manifest.tracked_files.append(file_key)
        if file_key not in self.manifest.files:
            self.manifest.files[file_key] = FileEntry(
                file_name=file_name,
                version="",
                last_modified="",
            )

    def set_page_entry(
        self,
        file_key: str,
        page_node_id: str,
        entry: PageEntry,
    ) -> None:
        """Update or insert a page entry for a tracked file."""
        if file_key in self.manifest.files:
            self.manifest.files[file_key].pages[page_node_id] = entry

    def should_skip_page(self, page_name: str) -> bool:
        """Return True if page_name matches any skip_pages glob pattern (case-insensitive)."""
        name_lower = page_name.lower()
        return any(
            fnmatch.fnmatch(name_lower, pattern.lower()) for pattern in self.manifest.skip_pages
        )

    def set_file_meta(
        self,
        file_key: str,
        version: str,
        last_modified: str,
        last_checked_at: str,
        *,
        file_name: str | None = None,
    ) -> None:
        """Update file-level metadata after a successful check."""
        if file_key in self.manifest.files:
            entry = self.manifest.files[file_key]
            if file_name:
                entry.file_name = file_name
            entry.version = version
            entry.last_modified = last_modified
            entry.last_checked_at = last_checked_at
