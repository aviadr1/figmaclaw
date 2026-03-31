"""Pydantic schema for the YAML frontmatter in figmaclaw-rendered markdown files.

Policy: any structured information needed by machines goes in the frontmatter.
The frontmatter is YAML; its schema is enforced by these Pydantic models.

Example rendered frontmatter:

    ---
    figmaclaw:
      file_key: hOV4QM
      page_node_id: "7741:45837"
      page_hash: deadbeef12345678
    frames:
      welcome screen: The welcome screen.
      permissions screen: Asks for camera access.
    ---
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FigmaclawMeta(BaseModel):
    """Identity fields for a rendered figmaclaw page."""

    file_key: str
    page_node_id: str
    page_hash: str


class FigmaPageFrontmatter(BaseModel):
    """Root schema for the YAML frontmatter block in a figmaclaw .md file."""

    figmaclaw: FigmaclawMeta
    frames: dict[str, str] = Field(default_factory=dict)
    flows: list[list[str]] = Field(default_factory=list)  # [[src_node_id, dst_node_id], ...]
