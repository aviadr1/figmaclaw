"""Body/frontmatter contract validation for figmaclaw page markdown.

This module enforces the key safety invariant:
frontmatter ``frames`` is authoritative, and the body frame tables must
contain exactly those node IDs (no missing, no extras, no duplicates).
"""

from __future__ import annotations

import pydantic

from figmaclaw.figma_md_parse import parse_sections
from figmaclaw.figma_parse import split_frontmatter


class BodyValidationResult(pydantic.BaseModel):
    """Structured result for body/frontmatter contract validation."""

    model_config = pydantic.ConfigDict(frozen=True)

    ok: bool
    missing_node_ids: list[str] = pydantic.Field(default_factory=list)
    extra_node_ids: list[str] = pydantic.Field(default_factory=list)
    duplicate_node_ids: list[str] = pydantic.Field(default_factory=list)

    def messages(self) -> list[str]:
        """Human-readable, stable error messages suitable for CLI output."""
        out: list[str] = []
        if self.missing_node_ids:
            out.append(f"missing frame rows for node_ids: {', '.join(self.missing_node_ids)}")
        if self.extra_node_ids:
            out.append(
                f"unexpected frame rows not in frontmatter: {', '.join(self.extra_node_ids)}"
            )
        if self.duplicate_node_ids:
            out.append(f"duplicate frame rows in body: {', '.join(self.duplicate_node_ids)}")
        return out


def body_frame_node_ids(body: str) -> list[str]:
    """Return frame node IDs from body tables in document order."""
    node_ids: list[str] = []
    for section in parse_sections(body):
        node_ids.extend(frame.node_id for frame in section.frames)
    return node_ids


def validate_body_against_frames(body: str, expected_frames: list[str]) -> BodyValidationResult:
    """Validate that body tables exactly match expected frame node IDs."""
    actual = body_frame_node_ids(body)

    actual_set = set(actual)
    expected_set = set(expected_frames)

    missing = [node_id for node_id in expected_frames if node_id not in actual_set]

    seen_extra: set[str] = set()
    extras: list[str] = []
    for node_id in actual:
        if node_id not in expected_set and node_id not in seen_extra:
            extras.append(node_id)
            seen_extra.add(node_id)

    seen: set[str] = set()
    dup_seen: set[str] = set()
    duplicates: list[str] = []
    for node_id in actual:
        if node_id in seen and node_id not in dup_seen:
            duplicates.append(node_id)
            dup_seen.add(node_id)
        seen.add(node_id)

    ok = not missing and not extras and not duplicates
    return BodyValidationResult(
        ok=ok,
        missing_node_ids=missing,
        extra_node_ids=extras,
        duplicate_node_ids=duplicates,
    )


def validate_markdown_contract(md: str, expected_frames: list[str]) -> BodyValidationResult:
    """Validate full markdown text by splitting frontmatter and checking body rows."""
    parts = split_frontmatter(md)
    if parts is None:
        return BodyValidationResult(ok=False)
    _, body = parts
    return validate_body_against_frames(body, expected_frames)
