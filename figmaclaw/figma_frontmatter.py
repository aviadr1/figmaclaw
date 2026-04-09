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
    enriched_schema_version: 1
    raw_frames: {'11:1': {raw: 3, ds: [AvatarV2, ButtonV2]}}
    ---

Example rendered frontmatter (component section):

    ---
    file_key: AZswXf
    page_node_id: '5678:1234'
    section_node_id: '20:1'
    frames: ['30:1', '30:2']
    component_set_keys: {ButtonV2: a1b2c3d4e5f67890, IconV2: b2c3d4e5f6789012}
    ---

Backward compatibility: the old v1 format stored frames as a dict
{node_id: description}. The validator normalizes this to a list of node IDs.

New fields (added incrementally, backward-compatible via empty defaults):
- component_set_keys: written to component section .md files by the pull pass.
  Maps component-set name → Figma key for use with importComponentSetByKeyAsync().
- raw_frames: written to screen page .md files by the pull pass.
  Sparse dict of frames that have at least one raw (non-INSTANCE) direct child.
  Frames absent from this dict are fully componentized. Used by audit skills to
  skip get_design_context calls on clean frames.
- enriched_schema_version: written by mark-enriched. Tracks which prompt version
  produced the LLM body. Used by inspect to surface MUST/SHOULD re-enrichment.

Schema version tracking
-----------------------
Two independent version numbers track whether a file/page needs updating:

  CURRENT_PULL_SCHEMA_VERSION (per-file in manifest)
      Bump when pull_file starts writing new frontmatter fields.
      Files below this version get frontmatter re-written on next pull —
      body is NEVER touched, no LLM triggered.

  CURRENT_ENRICHMENT_SCHEMA_VERSION (per-page in frontmatter, written by mark-enriched)
  MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION
      Bump CURRENT when prompt changes (any improvement or format change).
      Bump MIN_REQUIRED too when old output is structurally wrong (MUST re-enrich).
      Leave MIN_REQUIRED lower for quality improvements (SHOULD re-enrich).

Pull schema changelog:
  v1: initial — frames, flows, enriched_*
  v2: added raw_frames (screen pages), component_set_keys (component sections)
  v3: added raw_tokens frontmatter summary + sidecar .tokens.json files

Enrichment schema changelog:
  v1: initial enrichment format — frame table + page summary + Mermaid flows
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

# Pull-pass schema version. Bump when pull_file writes new frontmatter fields.
# Files in the manifest with pull_schema_version < this get frontmatter-refreshed
# on the next pull run even if Figma content is unchanged. Body is never touched.
CURRENT_PULL_SCHEMA_VERSION: int = 3

# Enrichment schema version. Bump when the LLM prompt or output format changes.
# Pages with enriched_schema_version < MIN_REQUIRED MUST be re-enriched (broken output).
# Pages with enriched_schema_version < CURRENT (but >= MIN_REQUIRED) SHOULD be
# re-enriched opportunistically (valid but outdated output).
# To make a bump "MUST": set both CURRENT and MIN_REQUIRED to the new value.
# To make a bump "SHOULD": bump only CURRENT, leave MIN_REQUIRED.
CURRENT_ENRICHMENT_SCHEMA_VERSION: int = 1
MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION: int = 1


class RawTokenCounts(BaseModel):
    """Per-frame token binding counts written by the pull pass into raw_tokens frontmatter.

    raw:   properties with no variable binding (hardcoded values)
    stale: properties bound to the deprecated OLD_Gigaverse library
    valid: properties correctly bound to the current DS library
    """

    raw: int = 0
    stale: int = 0
    valid: int = 0


class FrameComposition(BaseModel):
    """Composition signals for a single screen frame.

    Written by the pull pass into the raw_frames frontmatter field.
    Only frames with at least one raw (non-INSTANCE) direct child are included
    in raw_frames — absence means the frame is fully componentized.

    raw: count of non-INSTANCE direct children (FRAME, GROUP, RECTANGLE, TEXT, etc.)
    ds:  names of INSTANCE direct children (DS components already embedded),
         with duplicates preserved so [ButtonV2, ButtonV2] means two separate instances.
    """

    raw: int
    ds: list[str] = Field(default_factory=list)


class FigmaPageFrontmatter(BaseModel):
    """Root schema for the YAML frontmatter block in a figmaclaw .md file."""

    file_key: str = ""
    page_node_id: str = ""
    section_node_id: str | None = None  # set for component library .md files only

    # v2: list of frame node IDs (what screens exist). No descriptions.
    frames: list[str] = Field(default_factory=list)

    # Each flow edge is exactly [src_node_id, dst_node_id]
    flows: list[Annotated[list[str], Field(min_length=2, max_length=2)]] = Field(
        default_factory=list
    )

    # Enrichment tracking — set by mark-enriched, read by inspect --needs-enrichment
    enriched_hash: str | None = None  # page_hash at time of last enrichment
    enriched_at: str | None = None  # ISO timestamp of last enrichment
    enriched_frame_hashes: dict[str, str] = Field(
        default_factory=dict
    )  # {node_id: frame_hash} at enrichment
    # 0 = pre-versioning or never enriched. Set to CURRENT_ENRICHMENT_SCHEMA_VERSION by mark-enriched.
    enriched_schema_version: int = 0

    # Pull-pass composition signals (written by pull, never by enrich — no re-enrichment triggered)
    # component_set_keys: set on component section .md files only. Maps component-set name → Figma key.
    component_set_keys: dict[str, str] = Field(default_factory=dict)
    # raw_frames: sparse dict of frames with raw children. Absent frames are fully componentized.
    raw_frames: dict[str, FrameComposition] = Field(default_factory=dict)
    # raw_tokens: sparse dict of frames with unbound token properties (raw or stale).
    # Absent frames have zero issues. Full per-node detail lives in .tokens.json sidecar.
    raw_tokens: dict[str, RawTokenCounts] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_frames(cls, data: Any) -> Any:
        """Accept old v1 format: frames as dict {node_id: description} → extract keys as list."""
        if isinstance(data, dict) and isinstance(data.get("frames"), dict):
            data = dict(data)  # don't mutate caller's dict
            data["frames"] = list(data["frames"].keys())
        return data

    @model_validator(mode="after")
    def _require_both_ids_or_neither(self) -> FigmaPageFrontmatter:
        """file_key and page_node_id must both be set or both be absent."""
        has_key = bool(self.file_key)
        has_node = bool(self.page_node_id)
        if has_key != has_node:
            raise ValueError(
                "file_key and page_node_id must both be set or both be empty; "
                f"got file_key={self.file_key!r}, page_node_id={self.page_node_id!r}"
            )
        return self
