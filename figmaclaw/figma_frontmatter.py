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
  v4: added frame_sections (screen pages) — per-frame child position map for context frame building
  v5: extended frame_sections entries with per-section direct-child inventory:
      instances[] and raw_count (issue #38 coverage queries)
  v6: extended frame_sections inventory with stable instance_component_ids[] to
      support rename-robust coverage/autodiscovery queries
  v7: added unresolvable_frames field (tombstones for NO-PROGRESS frames),
      enforced frame-keyed key-set invariant at the _build_frontmatter
      chokepoint, and added pull-time orphan body-row pruning via
      iter_body_frame_rows (issue #121). Forcing a refresh cleans up
      existing files whose `frames` list shrank in a prior pull but whose
      enriched_frame_hashes / body-table rows retained the orphans.

Enrichment schema changelog:
  v1: initial enrichment format — frame table + page summary + Mermaid flows
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator, model_validator

# node_id shape: "<number>:<number>" (Figma's canonical node ID format).
# Used to validate tombstone keys in unresolvable_frames so a malicious
# or accidentally-corrupted commit can't inject non-Figma strings.
_NODE_ID_RE = re.compile(r"^\d+:\d+$")

# Maximum length of a tombstone hash value. The manifest stores content
# hashes as lowercase hex (compute_frame_hash uses 16-char blake2b output);
# we accept up to 64 chars to allow future hash upgrades without a schema
# bump, but reject unbounded strings that could bloat the frontmatter.
_MAX_TOMBSTONE_HASH_LEN = 64
_TOMBSTONE_HASH_RE = re.compile(r"^[0-9a-f]+$")

# Pull-pass schema version. Bump when pull_file writes new frontmatter fields.
# Files in the manifest with pull_schema_version < this get frontmatter-refreshed
# on the next pull run even if Figma content is unchanged. Body is never touched
# (except for the narrow orphan-row prune in figmaclaw#121 — structural, not prose).
CURRENT_PULL_SCHEMA_VERSION: int = 8

# Enrichment schema version. Bump when the LLM prompt or output format changes.
# Pages with enriched_schema_version < MIN_REQUIRED MUST be re-enriched (broken output).
# Pages with enriched_schema_version < CURRENT (but >= MIN_REQUIRED) SHOULD be
# re-enriched opportunistically (valid but outdated output).
# To make a bump "MUST": set both CURRENT and MIN_REQUIRED to the new value.
# To make a bump "SHOULD": bump only CURRENT, leave MIN_REQUIRED.
CURRENT_ENRICHMENT_SCHEMA_VERSION: int = 1
MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION: int = 1


class SectionNode(BaseModel):
    """Position and identity of one direct child within a screen frame.

    Written by the pull pass into the frame_sections frontmatter field.
    Provides the section map needed to build composite "Usage in Context" frames
    without a separate REST API call — see figmaclaw issue #38.

    node_id: Figma node ID of the child (e.g. '7424:16018')
    name:    display name of the child node
    x, y:   position relative to the parent frame's top-left corner
    w, h:   width and height in Figma units
    """

    node_id: str
    name: str
    x: int
    y: int
    w: int
    h: int
    # v5-ish extension of the frame_sections payload (schema version tracked at file level):
    # direct-child inventory of this section node.
    # instances keeps duplicates to preserve multiplicity (N x ButtonV2).
    instances: list[str] = Field(default_factory=list)
    # Stable component IDs for INSTANCE children (Figma's componentId field).
    # Duplicates preserved so multiplicity is queryable.
    # Unlike display names, these remain stable across renames.
    instance_component_ids: list[str] = Field(default_factory=list)
    # non-INSTANCE direct children count under this section node.
    raw_count: int = 0


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
    # frame_sections: per-frame map of direct children with their positions.
    # Dense (all screen frames included). Used by build-context to construct composite
    # "Usage in Context" frames and to answer component-coverage questions without
    # extra REST API calls. See figmaclaw issues #35 and #38.
    frame_sections: dict[str, list[SectionNode]] = Field(default_factory=dict)

    # unresolvable_frames: terminal-state tombstones for frames the LLM
    # has confirmed it cannot describe at the current content hash (e.g.
    # because no screenshot is available and the raw composition doesn't
    # give enough context). Maps node_id → frame_hash at the time the
    # tombstone was recorded.
    #
    # Contract:
    # - A node_id appears here AT MOST when the same-run NO-PROGRESS guard
    #   fires on it (figmaclaw#117) — i.e. the LLM tried to resolve the row
    #   and failed.
    # - A body row for a tombstoned node_id is treated as NOT pending by
    #   ``pending_frame_node_ids`` *while* its current manifest hash equals
    #   the recorded tombstone hash. When Figma content changes and the
    #   hash moves, the tombstone auto-invalidates and the row becomes
    #   pending again (one retry per content change).
    # - Pruned to ``⊆ frames`` by the frontmatter-write chokepoint, same
    #   as every other frame-keyed dict (figmaclaw#121).
    unresolvable_frames: dict[str, str] = Field(default_factory=dict)

    @field_validator("unresolvable_frames")
    @classmethod
    def _validate_unresolvable_frames(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject malformed tombstones — node IDs must look like Figma node IDs
        and hash values must be short lowercase hex.

        See security review in figmaclaw#121: without this, a malicious
        actor (or accidental corruption) could inject arbitrary strings
        as "tombstones" to make the enricher skip frames indefinitely.
        Strict shape validation closes that vector at parse time.
        """
        if not value:
            return value
        for node_id, hash_value in value.items():
            if not _NODE_ID_RE.match(node_id):
                raise ValueError(
                    f"unresolvable_frames key {node_id!r} is not a valid Figma "
                    f"node_id (expected '<number>:<number>')"
                )
            if not isinstance(hash_value, str):
                raise ValueError(
                    f"unresolvable_frames[{node_id!r}] must be a string, "
                    f"got {type(hash_value).__name__}"
                )
            if not 0 < len(hash_value) <= _MAX_TOMBSTONE_HASH_LEN:
                raise ValueError(
                    f"unresolvable_frames[{node_id!r}] hash length "
                    f"{len(hash_value)} out of range (1..{_MAX_TOMBSTONE_HASH_LEN})"
                )
            if not _TOMBSTONE_HASH_RE.match(hash_value):
                raise ValueError(
                    f"unresolvable_frames[{node_id!r}] is not lowercase hex: {hash_value!r}"
                )
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_frames(cls, data: Any) -> Any:
        """Accept old v1 format: frames as dict {node_id: description} → extract keys as list."""
        if isinstance(data, dict) and isinstance(data.get("frames"), dict):
            data = dict(data)  # don't mutate caller's dict
            data["frames"] = list(data["frames"].keys())
        return data

    @model_validator(mode="after")
    def _cap_unresolvable_frames_to_frames(self) -> FigmaPageFrontmatter:
        """Tombstone set must be ⊆ frames.

        The build-time chokepoint in ``_build_frontmatter`` strips
        orphan tombstones on write, but a hand-edited file could land on
        disk with extras. Validate on parse so downstream code never
        sees a tombstone for a non-existent frame.
        """
        if not self.unresolvable_frames:
            return self
        frame_set = set(self.frames)
        orphans = [nid for nid in self.unresolvable_frames if nid not in frame_set]
        if orphans:
            # Prune rather than error — the invariant is "cannot surface
            # orphans", not "cannot tolerate them". Logs already warn via
            # the key-set chokepoint on the next write.
            self.unresolvable_frames = {
                nid: h for nid, h in self.unresolvable_frames.items() if nid in frame_set
            }
        return self

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
