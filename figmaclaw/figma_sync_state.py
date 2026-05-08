"""Manages .figma-sync/manifest.json — the persistent state for figmaclaw.

The manifest tracks which Figma files are synced, their current API versions,
and per-page structural hashes. It is committed to the repo alongside the
generated .md files — never .gitignored.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from figmaclaw.figma_utils import write_json_if_changed

MANIFEST_TIMESTAMP_KEYS = frozenset({"last_checked_at", "last_refreshed_at"})
CURRENT_MANIFEST_SCHEMA_VERSION = 2


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
    # 0/None = pre page-level schema tracking. When absent in legacy manifests,
    # callers fall back to the parent FileEntry.pull_schema_version.
    pull_schema_version: int | None = None
    component_md_paths: list[str] = Field(default_factory=list)
    component_schema_versions: dict[str, int] = Field(default_factory=dict)
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

    @model_validator(mode="after")
    def _backfill_page_schema_versions(self) -> FileEntry:
        """Migrate legacy page entries to explicit page schema state in memory."""
        for page in self.pages.values():
            if page.pull_schema_version is None:
                page.pull_schema_version = self.pull_schema_version
            if page.component_md_paths:
                for rel in page.component_md_paths:
                    page.component_schema_versions.setdefault(rel, page.pull_schema_version or 0)
        return self


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

    schema_version: int = CURRENT_MANIFEST_SCHEMA_VERSION
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

    @model_validator(mode="after")
    def _migrate_manifest_schema(self) -> Manifest:
        """Upgrade legacy manifest shape without dropping tracked state."""
        self.schema_version = CURRENT_MANIFEST_SCHEMA_VERSION
        return self


def effective_page_pull_schema_version(file_entry: FileEntry, page_entry: PageEntry) -> int:
    """Return page schema version with legacy file-level fallback."""
    if page_entry.pull_schema_version is not None:
        return page_entry.pull_schema_version
    return file_entry.pull_schema_version


def effective_component_pull_schema_version(page_entry: PageEntry, component_rel: str) -> int:
    """Return component artifact schema version with page-level fallback."""
    return page_entry.component_schema_versions.get(
        component_rel, page_entry.pull_schema_version or 0
    )


def page_schema_is_current(
    file_entry: FileEntry,
    page_entry: PageEntry,
    *,
    current_pull_schema_version: int,
) -> bool:
    """True when one page and its component artifacts are at current pull schema."""
    page_version = effective_page_pull_schema_version(file_entry, page_entry)
    if page_version < current_pull_schema_version:
        return False
    return all(
        effective_component_pull_schema_version(page_entry, rel) >= current_pull_schema_version
        for rel in page_entry.component_md_paths
    )


def file_schema_is_current(
    file_entry: FileEntry,
    *,
    current_pull_schema_version: int,
    should_skip_page: Callable[[str], bool] | None = None,
) -> bool:
    """True when file and all non-skipped page artifacts are at current pull schema."""
    if file_entry.pull_schema_version < current_pull_schema_version:
        return False
    for page_entry in file_entry.pages.values():
        if should_skip_page is not None and should_skip_page(page_entry.page_name):
            continue
        if not page_schema_is_current(
            file_entry,
            page_entry,
            current_pull_schema_version=current_pull_schema_version,
        ):
            return False
    return True


def file_has_pull_schema_debt(
    file_entry: FileEntry,
    *,
    current_pull_schema_version: int,
    should_skip_page: Callable[[str], bool] | None = None,
) -> bool:
    """Return True when any file/page/component pull schema state is stale."""
    return not file_schema_is_current(
        file_entry,
        current_pull_schema_version=current_pull_schema_version,
        should_skip_page=should_skip_page,
    )


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
        self.manifest.schema_version = CURRENT_MANIFEST_SCHEMA_VERSION
        self._normalize_schema_versions()
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

    def _normalize_schema_versions(self) -> None:
        """Write explicit page/component schema versions for legacy in-memory entries."""
        for file_entry in self.manifest.files.values():
            for page_entry in file_entry.pages.values():
                if page_entry.pull_schema_version is None:
                    page_entry.pull_schema_version = file_entry.pull_schema_version
                for rel in page_entry.component_md_paths:
                    page_entry.component_schema_versions.setdefault(
                        rel, page_entry.pull_schema_version or 0
                    )
