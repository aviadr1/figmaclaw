"""Body/frontmatter contract validation for figmaclaw page markdown.

This module enforces the key safety invariant:
frontmatter ``frames`` is authoritative, and the body frame tables must
contain exactly those node IDs (no missing, no extras, no duplicates).
"""

from __future__ import annotations

import pydantic

from figmaclaw.figma_parse import split_frontmatter
from figmaclaw.figma_schema import (
    parse_frame_row,
    parse_section_heading,
    render_frame_table_header,
    render_variant_table_header,
)


class BodyValidationResult(pydantic.BaseModel):
    """Structured result for body/frontmatter contract validation."""

    model_config = pydantic.ConfigDict(frozen=True)

    ok: bool
    missing_node_ids: list[str] = pydantic.Field(default_factory=list)
    extra_node_ids: list[str] = pydantic.Field(default_factory=list)
    duplicate_node_ids: list[str] = pydantic.Field(default_factory=list)
    duplicate_frontmatter_node_ids: list[str] = pydantic.Field(default_factory=list)

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
        if self.duplicate_frontmatter_node_ids:
            out.append(
                "duplicate frame node_ids in frontmatter: "
                f"{', '.join(self.duplicate_frontmatter_node_ids)}"
            )
        return out


def _duplicate_node_ids(node_ids: list[str]) -> list[str]:
    """Return node IDs that appear more than once, preserving first duplicate order."""
    seen: set[str] = set()
    dup_seen: set[str] = set()
    duplicates: list[str] = []
    for node_id in node_ids:
        if node_id in seen and node_id not in dup_seen:
            duplicates.append(node_id)
            dup_seen.add(node_id)
        seen.add(node_id)
    return duplicates


def body_frame_node_ids(body: str) -> list[str]:
    """Return frame node IDs from canonical figmaclaw tables in frame sections only."""
    lines = body.splitlines()
    frame_header, frame_separator = render_frame_table_header()
    variant_header, variant_separator = render_variant_table_header()

    canonical_tables = {
        (frame_header, frame_separator),
        (variant_header, variant_separator),
    }

    # Discover frame-section ranges using heading parser; prose sections have empty node_id.
    section_starts: list[int] = []
    for i, line in enumerate(lines):
        parsed = parse_section_heading(line)
        if parsed is not None and parsed.node_id:
            section_starts.append(i)

    if not section_starts:
        return []

    node_ids: list[str] = []

    for idx, start in enumerate(section_starts):
        end = section_starts[idx + 1] if idx + 1 < len(section_starts) else len(lines)
        in_fence = False
        i = start + 1
        while i < end:
            stripped = lines[i].strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                i += 1
                continue
            if in_fence:
                i += 1
                continue

            if i + 1 < end and (stripped, lines[i + 1].strip()) in canonical_tables:
                # Parse canonical frame rows until table ends.
                i += 2
                while i < end:
                    row_line = lines[i]
                    row_stripped = row_line.strip()
                    if row_stripped.startswith("```"):
                        break
                    if not row_stripped:
                        break
                    row = parse_frame_row(row_line)
                    if row is None:
                        break
                    node_ids.append(row.node_id)
                    i += 1
                continue

            i += 1

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

    duplicates = _duplicate_node_ids(actual)
    expected_duplicates = _duplicate_node_ids(expected_frames)

    ok = not missing and not extras and not duplicates and not expected_duplicates
    return BodyValidationResult(
        ok=ok,
        missing_node_ids=missing,
        extra_node_ids=extras,
        duplicate_node_ids=duplicates,
        duplicate_frontmatter_node_ids=expected_duplicates,
    )


def validate_markdown_contract(md: str, expected_frames: list[str]) -> BodyValidationResult:
    """Validate full markdown text by checking body rows against frontmatter frames."""
    parts = split_frontmatter(md)
    if parts is None:
        return BodyValidationResult(ok=False)

    _, body = parts
    return validate_body_against_frames(body, expected_frames)
