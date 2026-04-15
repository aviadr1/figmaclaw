"""Path conventions for figmaclaw output files."""

from __future__ import annotations

import re
from pathlib import Path


def slugify(text: str, fallback: str = "untitled") -> str:
    """Convert text to a URL-friendly slug.

    Returns fallback if the result would be empty (e.g. text is "---").
    """
    result = text.lower().strip()
    result = re.sub(r"[^\w\s-]", "", result)
    result = re.sub(r"[\s_]+", "-", result)
    result = re.sub(r"-+", "-", result)
    result = result.strip("-")
    return result or fallback


def page_path(file_slug: str, page_slug: str) -> str:
    """Return the repo-relative path for a page markdown file.

    Example: figma/web-app/pages/onboarding-7741-45837.md
    """
    return f"figma/{file_slug}/pages/{page_slug}.md"


def file_slug_for_key(
    file_name: str,
    file_key: str,
    *,
    tracked_file_names: dict[str, str] | None = None,
) -> str:
    """Return a deterministic slug for one tracked file.

    The full file key is always included so paths are globally unique and never
    depend on collision detection order (e.g. ``web-app-hOV4...``).
    """
    _ = tracked_file_names  # backward-compatible signature; intentionally unused
    base_slug = slugify(file_name, fallback=file_key)
    return f"{base_slug}-{file_key}"


def screenshot_cache_path(repo_dir: str | Path, file_key: str, node_id: str) -> Path:
    """Return the local cache path for a frame screenshot.

    Saves under .figma-cache/screenshots/{file_key}/{node_id}.png
    Colons in node IDs are replaced with hyphens for filesystem safety.
    """
    safe_node_id = node_id.replace(":", "-")
    return Path(repo_dir) / ".figma-cache" / "screenshots" / file_key / f"{safe_node_id}.png"


def component_path(file_slug: str, section_slug: str) -> str:
    """Return the repo-relative path for a component library section markdown file.

    Example: figma/design-system/components/buttons-10-1.md
    """
    return f"figma/{file_slug}/components/{section_slug}.md"


def census_path(file_slug: str) -> str:
    """Return the repo-relative path for a file's component census markdown file.

    The underscore prefix distinguishes it from pull-generated page/component files.

    Example: figma/design-system/_census.md
    """
    return f"figma/{file_slug}/_census.md"


def token_sidecar_path(screen_md: Path) -> Path:
    """Return the .tokens.json sidecar path for a screen markdown file path."""
    return screen_md.with_suffix(".tokens.json")
