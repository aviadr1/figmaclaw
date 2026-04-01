"""Tests for commands/page_tree.py.

INVARIANTS:
- page-tree --json outputs a dict with the expected top-level schema fields
- page-tree --json includes all sections and frames from the .md file
- page-tree exits with code 1 when any frame is missing a description
- page-tree exits with code 0 when all frames have descriptions
- page-tree --missing-only includes only frames without descriptions
- page-tree --json --missing-only omits sections where all frames are described
- page-tree exits with code 2 for a file with no figmaclaw frontmatter
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import render_page
from figmaclaw.figma_sync_state import PageEntry
from figmaclaw.main import cli


def _make_page(with_descriptions: bool = False) -> FigmaPage:
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description="Welcome screen." if with_descriptions else ""),
        FigmaFrame(node_id="11:2", name="permissions", description="Permissions." if with_descriptions else ""),
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
    md = render_page(page, _make_entry())
    p = tmp_path / "page.md"
    p.write_text(md)
    return p


def test_page_tree_json_output_has_expected_schema(tmp_path: Path) -> None:
    """INVARIANT: --json output is a dict with file_key, page_node_id, total_frames, missing_descriptions, sections."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "page-tree", str(md_path), "--json",
    ])

    data = json.loads(result.output)
    assert "file_key" in data
    assert "page_node_id" in data
    assert "total_frames" in data
    assert "missing_descriptions" in data
    assert "sections" in data
    assert isinstance(data["sections"], list)


def test_page_tree_json_carries_file_key_and_page_node_id(tmp_path: Path) -> None:
    """INVARIANT: --json output carries the file_key and page_node_id from frontmatter."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "page-tree", str(md_path), "--json",
    ])

    data = json.loads(result.output)
    assert data["file_key"] == "abc123"
    assert data["page_node_id"] == "7741:45837"


def test_page_tree_json_includes_all_frames(tmp_path: Path) -> None:
    """INVARIANT: --json output includes every frame from the .md file."""
    md_path = _write_md(tmp_path, _make_page())
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "page-tree", str(md_path), "--json",
    ])

    data = json.loads(result.output)
    assert data["total_frames"] == 2
    node_ids = {f["node_id"] for s in data["sections"] for f in s["frames"]}
    assert node_ids == {"11:1", "11:2"}


def test_page_tree_exit_code_1_when_missing_descriptions(tmp_path: Path) -> None:
    """INVARIANT: page-tree exits with code 1 when any frame lacks a description."""
    md_path = _write_md(tmp_path, _make_page(with_descriptions=False))
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "page-tree", str(md_path), "--json",
    ])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["missing_descriptions"] == 2


def test_page_tree_exit_code_0_when_all_described(tmp_path: Path) -> None:
    """INVARIANT: page-tree exits with code 0 when all frames have descriptions."""
    md_path = _write_md(tmp_path, _make_page(with_descriptions=True))
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "page-tree", str(md_path), "--json",
    ])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["missing_descriptions"] == 0


def test_page_tree_missing_only_excludes_described_frames(tmp_path: Path) -> None:
    """INVARIANT: --missing-only filters to frames that need descriptions only."""
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description="Already described."),
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
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "page-tree", str(md_path), "--json", "--missing-only",
    ])

    data = json.loads(result.output)
    all_frame_ids = {f["node_id"] for s in data["sections"] for f in s["frames"]}
    assert "11:2" in all_frame_ids
    assert "11:1" not in all_frame_ids


def test_page_tree_exit_code_2_for_non_figmaclaw_file(tmp_path: Path) -> None:
    """INVARIANT: page-tree exits with code 2 when the file has no figmaclaw frontmatter."""
    md_path = tmp_path / "plain.md"
    md_path.write_text("# Plain markdown\n\nNo frontmatter here.\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "page-tree", str(md_path),
    ])

    assert result.exit_code == 2
