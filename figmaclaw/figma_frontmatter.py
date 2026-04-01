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

from typing import Annotated

from pydantic import BaseModel, Field, model_validator


class FigmaPageFrontmatter(BaseModel):
    """Root schema for the YAML frontmatter block in a figmaclaw .md file."""

    file_key: str = ""
    page_node_id: str = ""
    section_node_id: str | None = None  # set for component library .md files only
    frames: dict[str, str] = Field(default_factory=dict)
    # Each flow edge is exactly [src_node_id, dst_node_id]
    flows: list[Annotated[list[str], Field(min_length=2, max_length=2)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_both_ids_or_neither(self) -> "FigmaPageFrontmatter":
        """file_key and page_node_id must both be set or both be absent."""
        has_key = bool(self.file_key)
        has_node = bool(self.page_node_id)
        if has_key != has_node:
            raise ValueError(
                "file_key and page_node_id must both be set or both be empty; "
                f"got file_key={self.file_key!r}, page_node_id={self.page_node_id!r}"
            )
        return self
