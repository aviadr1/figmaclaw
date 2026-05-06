"""Tests for source-system lifecycle provenance (canon TC-12 / D15)."""

from __future__ import annotations

from types import SimpleNamespace

from figmaclaw.source_context import classify_source_lifecycle, source_context_from_manifest_entry


def test_classify_source_lifecycle_marks_archive_projects() -> None:
    assert classify_source_lifecycle("New Live Experience", "ARCHIVE") == "archived"
    assert classify_source_lifecycle("Design System", "📦 Old Systems") == "archived"


def test_classify_source_lifecycle_marks_active_when_named_source_exists() -> None:
    assert classify_source_lifecycle("Tap In Design System", "Design System") == "active"


def test_classify_source_lifecycle_marks_unknown_without_source_names() -> None:
    assert classify_source_lifecycle(None, "") == "unknown"


def test_source_context_from_manifest_entry_preserves_project_fields() -> None:
    entry = SimpleNamespace(
        file_name="Legacy Buttons",
        source_project_id="123",
        source_project_name="📦 Archive",
        source_lifecycle="archived",
    )

    context = source_context_from_manifest_entry(entry)

    assert context.project_id == "123"
    assert context.project_name == "📦 Archive"
    assert context.lifecycle == "archived"
