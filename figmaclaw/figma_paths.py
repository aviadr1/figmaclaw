"""Path conventions for figmaclaw output files."""

from __future__ import annotations

import re


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def page_path(file_key: str, page_slug: str) -> str:
    """Return the repo-relative path for a page markdown file.

    Example: figma/hOV4QM/pages/onboarding.md
    """
    return f"figma/{file_key}/pages/{page_slug}.md"
