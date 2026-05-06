"""Source-file provenance helpers for Figma registries.

Token and component registries are file-scope artifacts, but migration tools
need to know more than "this row came from file X." They also need the source
system's lifecycle so archived/legacy libraries can be used as migration
evidence without becoming the default target design system.
"""

from __future__ import annotations

from pydantic import BaseModel


class SourceContext(BaseModel):
    """Stable provenance attached to file-scope registry entries."""

    project_id: str | None = None
    project_name: str | None = None
    lifecycle: str = "unknown"


_ARCHIVE_MARKERS = (
    "archive",
    "archived",
    "\U0001f4e6",  # package emoji, commonly used for Figma archive projects.
)


def classify_source_lifecycle(*names: str | None) -> str:
    """Return ``archived`` when any known source label is archive-like.

    Figma's public file metadata does not expose a first-class archived flag.
    The team/project listing is therefore the best available source: if a file
    lives under an archive project, or the file name itself is archive-marked,
    registry consumers must treat it as historical evidence.
    """
    normalized = " ".join(name.lower() for name in names if name)
    if not normalized:
        return "unknown"
    if any(marker in normalized for marker in _ARCHIVE_MARKERS):
        return "archived"
    return "active"


def source_context_from_manifest_entry(entry: object | None) -> SourceContext:
    """Build source context from a manifest FileEntry-like object."""
    if entry is None:
        return SourceContext()
    project_id = getattr(entry, "source_project_id", None)
    project_name = getattr(entry, "source_project_name", None)
    lifecycle = getattr(entry, "source_lifecycle", None)
    if not lifecycle:
        lifecycle = classify_source_lifecycle(
            getattr(entry, "file_name", None),
            project_name,
        )
    return SourceContext(
        project_id=str(project_id) if project_id else None,
        project_name=str(project_name) if project_name else None,
        lifecycle=lifecycle,
    )
