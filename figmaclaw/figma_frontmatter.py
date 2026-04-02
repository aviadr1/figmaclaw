"""Pydantic schema for the YAML frontmatter in figmaclaw-rendered markdown files.

Policy: any structured information needed by machines goes in the frontmatter.
The frontmatter is YAML; its schema is enforced by these Pydantic models.

Example rendered frontmatter (v2 — screen page):

    ---
    file_key: hOV4QMBnDIG5s5OYkSrX9E
    page_node_id: '7741:45837'
    frames: ['11:1', '11:2']
    flows: [['11:1', '11:2']]
    enriched_hash: b39103d8ad45cd38
    enriched_at: '2026-04-01T12:00:00Z'
    enriched_frame_hashes: {'11:1': a3f2b7c1, '11:2': e4d9f8a2}
    ---

Example rendered frontmatter (component section):

    ---
    file_key: AZswXf
    page_node_id: '5678:1234'
    section_node_id: '20:1'
    frames: ['30:1', '30:2']
    ---

Backward compatibility: the old v1 format stored frames as a dict
{node_id: description}. The validator normalizes this to a list of node IDs.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator


class FigmaPageFrontmatter(BaseModel):
    """Root schema for the YAML frontmatter block in a figmaclaw .md file."""

    file_key: str = ""
    page_node_id: str = ""
    section_node_id: str | None = None  # set for component library .md files only

    # v2: list of frame node IDs (what screens exist). No descriptions.
    frames: list[str] = Field(default_factory=list)

    # Each flow edge is exactly [src_node_id, dst_node_id]
    flows: list[Annotated[list[str], Field(min_length=2, max_length=2)]] = Field(default_factory=list)

    # Enrichment tracking — set by mark-enriched, read by inspect --needs-enrichment
    enriched_hash: str | None = None  # page_hash at time of last enrichment
    enriched_at: str | None = None  # ISO timestamp of last enrichment
    enriched_frame_hashes: dict[str, str] = Field(default_factory=dict)  # {node_id: frame_hash} at enrichment

    @model_validator(mode="before")
    @classmethod
    def _normalize_frames(cls, data: Any) -> Any:
        """Accept old v1 format: frames as dict {node_id: description} → extract keys as list."""
        if isinstance(data, dict) and isinstance(data.get("frames"), dict):
            data = dict(data)  # don't mutate caller's dict
            data["frames"] = list(data["frames"].keys())
        return data

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
