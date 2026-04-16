"""Tests for commands/set_flows.py.

INVARIANTS:
- set-flows writes flows to the YAML frontmatter only
- set-flows does NOT modify the body
- set-flows preserves frames list and enrichment state
- set-flows exits 2 when frontmatter parse fails
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_parse import parse_flows, parse_frontmatter
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import PageEntry
from figmaclaw.main import cli


def _make_page() -> FigmaPage:
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description=""),
        FigmaFrame(node_id="11:2", name="permissions", description=""),
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


def test_set_flows_writes_flows_to_frontmatter(tmp_path: Path) -> None:
    """INVARIANT: set-flows stores flow edges in the YAML frontmatter."""
    md = scaffold_page(_make_page(), _make_entry())
    md_path = tmp_path / "page.md"
    md_path.write_text(md)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "set-flows",
            str(md_path),
            "--flows",
            json.dumps([["11:1", "11:2"]]),
        ],
    )
    assert result.exit_code == 0, result.output

    recovered = parse_flows(md_path.read_text())
    assert recovered == [("11:1", "11:2")]


def test_set_flows_preserves_frames_list(tmp_path: Path) -> None:
    """INVARIANT: set-flows does not modify the frames list."""
    md = scaffold_page(_make_page(), _make_entry())
    md_path = tmp_path / "page.md"
    md_path.write_text(md)

    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "set-flows",
            str(md_path),
            "--flows",
            json.dumps([["11:1", "11:2"]]),
        ],
    )

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert isinstance(fm.frames, list)
    assert "11:1" in fm.frames
    assert "11:2" in fm.frames


def test_set_flows_does_not_modify_body(tmp_path: Path) -> None:
    """INVARIANT: set-flows preserves the body byte-for-byte."""
    import frontmatter as _frontmatter

    md = scaffold_page(_make_page(), _make_entry())
    md_path = tmp_path / "page.md"
    md_path.write_text(md)

    original_body = _frontmatter.loads(md).content

    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "set-flows",
            str(md_path),
            "--flows",
            json.dumps([["11:1", "11:2"]]),
        ],
    )

    updated_body = _frontmatter.loads(md_path.read_text()).content
    assert updated_body == original_body


def test_set_flows_exit_2_on_bad_frontmatter(tmp_path: Path) -> None:
    """INVARIANT: set-flows exits 2 when the file has no figmaclaw frontmatter."""
    md_path = tmp_path / "plain.md"
    md_path.write_text("# No frontmatter\n\nJust markdown.\n")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "set-flows",
            str(md_path),
            "--flows",
            json.dumps([["1:1", "1:2"]]),
        ],
    )
    assert result.exit_code == 2


def test_set_flows_preserves_explicit_enrichment_schema_version(tmp_path: Path) -> None:
    """INVARIANT: set-flows preserves explicit enriched_schema_version in frontmatter."""
    md = scaffold_page(_make_page(), _make_entry())
    md_path = tmp_path / "page.md"
    md_path.write_text(md)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "set-flows",
            str(md_path),
            "--flows",
            json.dumps([["11:1", "11:2"]]),
        ],
    )
    assert result.exit_code == 0, result.output

    text = md_path.read_text()
    assert "enriched_schema_version: 0" in text


def test_set_flows_migrates_missing_enrichment_schema_version(tmp_path: Path) -> None:
    """INVARIANT: set-flows backfills enriched_schema_version when missing."""
    md_path = tmp_path / "page.md"
    md_path.write_text(
        """---
file_key: abc123
page_node_id: '1:1'
frames: ['11:1']
---

## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | desc |
"""
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "set-flows",
            str(md_path),
            "--flows",
            json.dumps([["11:1", "11:1"]]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "enriched_schema_version: 0" in md_path.read_text()
