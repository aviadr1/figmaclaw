"""Path conventions for figmaclaw output files."""

from __future__ import annotations

import re


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


def component_path(file_slug: str, section_slug: str) -> str:
    """Return the repo-relative path for a component library section markdown file.

    Example: figma/design-system/components/buttons-10-1.md
    """
    return f"figma/{file_slug}/components/{section_slug}.md"
