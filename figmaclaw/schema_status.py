"""Shared schema/version status helpers for pull and enrichment flows."""

from __future__ import annotations

from pydantic import BaseModel

from figmaclaw.figma_frontmatter import (
    CURRENT_ENRICHMENT_SCHEMA_VERSION,
    CURRENT_PULL_SCHEMA_VERSION,
    MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION,
)


class EnrichmentSchemaStatus(BaseModel):
    """Computed enrichment schema status flags for one page."""

    version: int
    must_update: bool
    should_update: bool


def is_pull_schema_stale(pull_schema_version: int) -> bool:
    """Return True when file pull schema version is behind current."""
    return pull_schema_version < CURRENT_PULL_SCHEMA_VERSION


def enrichment_schema_status(enriched_schema_version: int) -> EnrichmentSchemaStatus:
    """Return enrichment schema update flags for one page."""
    return EnrichmentSchemaStatus(
        version=enriched_schema_version,
        must_update=enriched_schema_version < MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION,
        should_update=enriched_schema_version < CURRENT_ENRICHMENT_SCHEMA_VERSION,
    )
