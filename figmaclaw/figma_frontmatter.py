"""Pydantic schema for the YAML frontmatter in figmaclaw-rendered markdown files.

Policy: any structured information needed by machines goes in the frontmatter.
The frontmatter is YAML; its schema is enforced by these Pydantic models.

Example rendered frontmatter (screen page):

    ---
    file_key: hOV4QMBnDIG5s5OYkSrX9E
    page_node_id: '7741:45837'
    frames: {'11:1': The welcome screen., '11:2': Asks for camera access.}
    flows: [['11:1', '11:2']]
    ---

Example rendered frontmatter (component section):

    ---
    file_key: AZswXf
    page_node_id: '5678:1234'
    section_node_id: '20:1'
    frames: {'30:1': Primary CTA button.}
    ---
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FigmaPageFrontmatter(BaseModel):
    """Root schema for the YAML frontmatter block in a figmaclaw .md file."""

    file_key: str = ""
    page_node_id: str = ""
    section_node_id: str | None = None  # set for component library .md files only
    frames: dict[str, str] = Field(default_factory=dict)
    flows: list[list[str]] = Field(default_factory=list)  # [[src_node_id, dst_node_id], ...]
