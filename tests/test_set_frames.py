"""Tests for commands/set_frames.py.

INVARIANTS:
- set-frames writes frame descriptions to the YAML frontmatter only
- set-frames does NOT modify table rows in the body
- Descriptions written via set-frames are recoverable via parse_frontmatter
- Descriptions containing pipe characters are stored correctly in frontmatter
- A node_id not present in the original file is merged into frontmatter without error
- --frames accepts a path to a .json file
- --flows replaces the flows list in frontmatter
- --summary sets the page summary paragraph
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_parse import parse_frontmatter, parse_flows
from figmaclaw.figma_render import render_page
from figmaclaw.figma_sync_state import PageEntry
from figmaclaw.main import cli


def _make_page(descriptions: dict[str, str] | None = None) -> FigmaPage:
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description=descriptions.get("11:1", "") if descriptions else ""),
        FigmaFrame(node_id="11:2", name="permissions", description=descriptions.get("11:2", "") if descriptions else ""),
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


def _write_md(tmp_path: Path, md: str) -> Path:
    p = tmp_path / "page.md"
    p.write_text(md)
    return p


def test_set_frames_writes_descriptions_to_frontmatter(tmp_path: Path) -> None:
    """INVARIANT: set-frames stores frame descriptions in the YAML frontmatter."""
    md = render_page(_make_page(), _make_entry())
    md_path = _write_md(tmp_path, md)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", json.dumps({"11:1": "Welcome screen.", "11:2": "Permissions prompt."}),
    ])

    assert result.exit_code == 0, result.output
    updated = md_path.read_text()
    fm = parse_frontmatter(updated)
    assert fm is not None
    assert fm.frames["11:1"] == "Welcome screen."
    assert fm.frames["11:2"] == "Permissions prompt."


def test_set_frames_does_not_modify_table_body(tmp_path: Path) -> None:
    """INVARIANT: set-frames writes frontmatter only — body table rows keep the original placeholder.

    Body prose is regenerated on the next `figmaclaw enrich` or `figmaclaw pull`, not here.
    """
    page = _make_page()  # no descriptions → placeholder in all rows
    md = render_page(page, _make_entry())
    assert "(no description yet)" in md
    md_path = _write_md(tmp_path, md)

    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", json.dumps({"11:1": "Welcome screen."}),
    ])

    updated = md_path.read_text()
    # The specific row for 11:1 must still have the placeholder — body untouched
    assert "| welcome | `11:1` | (no description yet) |" in updated
    # 11:2 also still has placeholder
    assert "| permissions | `11:2` | (no description yet) |" in updated
    # The description lives in frontmatter only
    fm = parse_frontmatter(updated)
    assert fm is not None
    assert fm.frames["11:1"] == "Welcome screen."


def test_set_frames_round_trip_via_parse_frontmatter(tmp_path: Path) -> None:
    """INVARIANT: Descriptions written by set-frames are fully recoverable via parse_frontmatter."""
    md = render_page(_make_page(), _make_entry())
    md_path = _write_md(tmp_path, md)

    descriptions = {"11:1": "The welcome.", "11:2": "Camera access."}
    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", json.dumps(descriptions),
    ])

    recovered = parse_frontmatter(md_path.read_text())
    assert recovered is not None
    for node_id, desc in descriptions.items():
        assert recovered.frames[node_id] == desc


def test_set_frames_description_with_pipe_stored_in_frontmatter(tmp_path: Path) -> None:
    """INVARIANT: Descriptions containing pipe characters are stored correctly in frontmatter."""
    md = render_page(_make_page(), _make_entry())
    md_path = _write_md(tmp_path, md)

    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", json.dumps({"11:1": "Options: A | B | C"}),
    ])

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert fm.frames["11:1"] == "Options: A | B | C"


def test_set_frames_unknown_node_id_added_to_frontmatter(tmp_path: Path) -> None:
    """INVARIANT: A node_id not present in the original file is merged into frontmatter without error."""
    md = render_page(_make_page(), _make_entry())
    md_path = _write_md(tmp_path, md)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", json.dumps({"99:99": "Unknown frame."}),
    ])

    assert result.exit_code == 0
    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert fm.frames["99:99"] == "Unknown frame."


def test_set_frames_file_path_frames_argument(tmp_path: Path) -> None:
    """INVARIANT: --frames accepts a path to a .json file containing the descriptions dict."""
    md = render_page(_make_page(), _make_entry())
    md_path = _write_md(tmp_path, md)
    frames_file = tmp_path / "descs.json"
    frames_file.write_text(json.dumps({"11:1": "From file."}))

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", str(frames_file),
    ])

    assert result.exit_code == 0
    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert fm.frames["11:1"] == "From file."


def test_set_frames_flows_replaces_frontmatter_flows(tmp_path: Path) -> None:
    """INVARIANT: --flows replaces the flows list in the frontmatter."""
    page = _make_page()
    md = render_page(page, _make_entry())
    md_path = _write_md(tmp_path, md)

    new_flows = [["11:1", "11:2"]]
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", json.dumps({}),
        "--flows", json.dumps(new_flows),
    ])

    assert result.exit_code == 0
    recovered = parse_flows(md_path.read_text())
    assert recovered == [("11:1", "11:2")]


def test_set_frames_merges_with_existing_descriptions(tmp_path: Path) -> None:
    """INVARIANT: set-frames merges new descriptions with existing ones — does not erase unmentioned frames."""
    page = _make_page(descriptions={"11:1": "Existing welcome."})
    md = render_page(page, _make_entry())
    md_path = _write_md(tmp_path, md)

    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "set-frames",
        str(md_path),
        "--frames", json.dumps({"11:2": "New permissions."}),
    ])

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert fm.frames["11:1"] == "Existing welcome."
    assert fm.frames["11:2"] == "New permissions."
