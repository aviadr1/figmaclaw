"""Tests for commands/inspect.py.

INVARIANTS:
- inspect --json outputs a dict with the expected top-level schema fields
- inspect --json includes all sections and frames from the .md file
- inspect always exits 0 on success (enrichment status is in JSON, not exit code)
- inspect --needs-enrichment shows only frames/pages that need enrichment
- inspect exits with code 2 for a file with no figmaclaw frontmatter
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import PageEntry
from figmaclaw.main import cli


def _make_page(with_descriptions: bool = False) -> FigmaPage:
    frames = [
        FigmaFrame(
            node_id="11:1",
            name="welcome",
            description="Welcome screen." if with_descriptions else "",
        ),
        FigmaFrame(
            node_id="11:2",
            name="permissions",
            description="Permissions." if with_descriptions else "",
        ),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    return FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id="7741:45837",
        page_name="Onboarding",
        page_slug="onboarding",
        figma_url="https://www.figma.com/design/abc123?node-id=7741-45837",
        sections=[section],
        flows=[],
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
    )


def _make_entry() -> PageEntry:
    return PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash="deadbeef12345678",
        last_refreshed_at="2026-03-31T00:00:00Z",
    )


def _write_md(tmp_path: Path, page: FigmaPage) -> Path:
    md = scaffold_page(page, _make_entry())
    p = tmp_path / "page.md"
    p.write_text(md)
    return p


def test_inspect_json_output_has_expected_schema(tmp_path: Path) -> None:
    """INVARIANT: --json output is a dict with file_key, page_node_id, total_frames, missing_descriptions, sections."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert "file_key" in data
    assert "page_node_id" in data
    assert "total_frames" in data
    assert "missing_descriptions" in data
    assert "sections" in data
    assert isinstance(data["sections"], list)


def test_inspect_json_carries_file_key_and_page_node_id(tmp_path: Path) -> None:
    """INVARIANT: --json output carries the file_key and page_node_id from frontmatter."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert data["file_key"] == "abc123"
    assert data["page_node_id"] == "7741:45837"


def test_inspect_json_includes_all_frames(tmp_path: Path) -> None:
    """INVARIANT: --json output includes every frame from the .md file."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert data["total_frames"] == 2
    node_ids = {f["node_id"] for s in data["sections"] for f in s["frames"]}
    assert node_ids == {"11:1", "11:2"}


def test_inspect_exit_0_with_placeholders(tmp_path: Path) -> None:
    """INVARIANT: inspect always exits 0 on success. Placeholder count is in JSON, not exit code."""
    md_path = _write_md(tmp_path, _make_page(with_descriptions=False))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["missing_descriptions"] == 2
    assert data["needs_enrichment"] is True


def test_inspect_exit_0_when_all_described(tmp_path: Path) -> None:
    """INVARIANT: inspect exits 0 with zero missing descriptions when all frames described."""
    md_path = _write_md(tmp_path, _make_page(with_descriptions=True))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["missing_descriptions"] == 0


def test_inspect_shows_needs_enrichment_for_unenriched_page(tmp_path: Path) -> None:
    """INVARIANT: inspect JSON shows needs_enrichment=true when page has no enriched_hash."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description=""),
        FigmaFrame(node_id="11:2", name="permissions", description=""),
    ]
    section = FigmaSection(node_id="10:1", name="onboarding", frames=frames)
    page = FigmaPage(
        file_key="abc123",
        file_name="Web App",
        page_node_id="7741:45837",
        page_name="Onboarding",
        page_slug="onboarding",
        figma_url="https://www.figma.com/design/abc123?node-id=7741-45837",
        sections=[section],
        flows=[],
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
    )
    md_path = _write_md(tmp_path, page)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["needs_enrichment"] is True  # no enriched_hash = needs enrichment


def test_inspect_json_per_section_pending_counts(tmp_path: Path) -> None:
    """INVARIANT: --json output includes per-section pending_frames counts."""
    md_path = _write_md(tmp_path, _make_page(with_descriptions=False))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert "total_sections" in data
    assert "pending_sections" in data
    assert data["total_sections"] == 1
    assert data["pending_sections"] == 1  # section has placeholders
    section = data["sections"][0]
    assert "pending_frames" in section
    assert "stale_frames" in section
    assert "total_frames" in section
    assert section["pending_frames"] == 2  # both frames have (no description yet)
    assert section["total_frames"] == 2


def test_inspect_json_per_section_zero_pending_when_described(tmp_path: Path) -> None:
    """INVARIANT: described sections have pending_frames=0."""
    md_path = _write_md(tmp_path, _make_page(with_descriptions=True))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert data["pending_sections"] == 0
    assert data["sections"][0]["pending_frames"] == 0


def test_inspect_json_section_threshold(tmp_path: Path) -> None:
    """INVARIANT: section_threshold is included in --json output."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert "section_threshold" in data
    assert data["section_threshold"] == 80


def test_inspect_exit_code_2_for_non_figmaclaw_file(tmp_path: Path) -> None:
    """INVARIANT: inspect exits with code 2 when the file has no figmaclaw frontmatter."""
    md_path = tmp_path / "plain.md"
    md_path.write_text("# Plain markdown\n\nNo frontmatter here.\n")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
        ],
    )

    assert result.exit_code == 2


# --- Schema version staleness reporting ---


def _write_md_with_enrichment(tmp_path: Path, enriched_schema_version: int = 0) -> Path:
    """Write a fully-described page .md that has been enriched at a given schema version."""
    page = _make_page(with_descriptions=True)
    entry = PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash="deadbeef12345678",
        last_refreshed_at="2026-03-31T00:00:00Z",
    )
    scaffold = scaffold_page(page, entry)
    # Inject enriched_hash and enriched_schema_version into frontmatter
    extra = f"enriched_hash: deadbeef12345678\nenriched_at: '2026-04-01T00:00:00Z'\nenriched_schema_version: {enriched_schema_version}"
    md_text = scaffold.replace("\n---\n", f"\n{extra}\n---\n", 1)
    p = tmp_path / "page.md"
    p.write_text(md_text)
    return p


def test_inspect_json_includes_schema_version_fields(tmp_path: Path) -> None:
    """INVARIANT: --json output includes all schema staleness fields."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert "pull_schema_stale" in data
    assert "pull_schema_version" in data
    assert "current_pull_schema_version" in data
    assert "enrichment_schema_version" in data
    assert "enrichment_must_update" in data
    assert "enrichment_should_update" in data
    assert "current_enrichment_schema_version" in data
    assert "min_required_enrichment_schema_version" in data


def test_inspect_reports_pull_schema_stale_when_manifest_version_behind(tmp_path: Path) -> None:
    """INVARIANT: pull_schema_stale=True when manifest file has pull_schema_version < current."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
    from figmaclaw.figma_sync_state import FigmaSyncState, FileEntry

    md_path = _write_md(tmp_path, _make_page())

    # Write a manifest with pull_schema_version=0 (behind current)
    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.files["abc123"] = FileEntry(
        file_name="Web App",
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
        pull_schema_version=0,
    )
    state.save()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert data["pull_schema_stale"] is True
    assert data["pull_schema_version"] == 0
    assert data["current_pull_schema_version"] == CURRENT_PULL_SCHEMA_VERSION


def test_inspect_reports_pull_schema_current_when_manifest_version_matches(tmp_path: Path) -> None:
    """INVARIANT: pull_schema_stale=False when manifest has pull_schema_version == current."""
    from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
    from figmaclaw.figma_sync_state import FigmaSyncState, FileEntry

    md_path = _write_md(tmp_path, _make_page())

    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.files["abc123"] = FileEntry(
        file_name="Web App",
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
        pull_schema_version=CURRENT_PULL_SCHEMA_VERSION,
    )
    state.save()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert data["pull_schema_stale"] is False


def test_inspect_reports_enrichment_must_update_when_below_required(tmp_path: Path) -> None:
    """INVARIANT: enrichment_must_update=True when enriched_schema_version < MIN_REQUIRED."""
    from figmaclaw.figma_frontmatter import MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION

    # Write a page enriched at version 0 (below any MIN_REQUIRED >= 1)
    md_path = _write_md_with_enrichment(tmp_path, enriched_schema_version=0)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    if MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION > 0:
        assert data["enrichment_must_update"] is True
        assert data["needs_enrichment"] is True
    else:
        # MIN_REQUIRED is 0 — nothing is ever forced. Test the invariant that
        # enrichment_must_update reflects the actual comparison.
        assert data["enrichment_must_update"] is False


def test_inspect_reports_enrichment_should_update_when_below_current_but_above_required(
    tmp_path: Path,
) -> None:
    """INVARIANT: enrichment_should_update=True when MIN_REQUIRED <= esv < CURRENT."""
    from figmaclaw.figma_frontmatter import (
        CURRENT_ENRICHMENT_SCHEMA_VERSION,
        MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION,
    )

    # This test is only meaningful when CURRENT > MIN_REQUIRED (SHOULD bucket exists)
    if CURRENT_ENRICHMENT_SCHEMA_VERSION <= MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION:
        pytest.skip("No SHOULD bucket: CURRENT == MIN_REQUIRED")

    # Enrich at MIN_REQUIRED — valid but not latest
    md_path = _write_md_with_enrichment(
        tmp_path, enriched_schema_version=MIN_REQUIRED_ENRICHMENT_SCHEMA_VERSION
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert data["enrichment_must_update"] is False  # valid output
    assert data["enrichment_should_update"] is True  # but outdated


def test_inspect_no_schema_staleness_when_all_current(tmp_path: Path) -> None:
    """INVARIANT: no staleness flags set when pull and enrichment schemas are both current."""
    from figmaclaw.figma_frontmatter import (
        CURRENT_ENRICHMENT_SCHEMA_VERSION,
        CURRENT_PULL_SCHEMA_VERSION,
    )
    from figmaclaw.figma_sync_state import FigmaSyncState, FileEntry

    md_path = _write_md_with_enrichment(
        tmp_path, enriched_schema_version=CURRENT_ENRICHMENT_SCHEMA_VERSION
    )

    state = FigmaSyncState(tmp_path)
    state.load()
    state.manifest.files["abc123"] = FileEntry(
        file_name="Web App",
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
        pull_schema_version=CURRENT_PULL_SCHEMA_VERSION,
    )
    state.save()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "inspect",
            str(md_path),
            "--json",
        ],
    )

    data = json.loads(result.output)
    assert data["pull_schema_stale"] is False
    assert data["enrichment_must_update"] is False
    assert data["enrichment_should_update"] is False
