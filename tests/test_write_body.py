"""Tests for commands/write_body.py.

INVARIANTS:
- write-body writes new body content below the frontmatter
- write-body preserves frontmatter byte-for-byte (BP-6)
- write-body fails for a file with no figmaclaw frontmatter
- write-body accepts body from --body flag or stdin
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter as _frontmatter
import pytest
from click.testing import CliRunner

from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import PageEntry
from figmaclaw.main import cli


def _make_page() -> FigmaPage:
    frames = [
        FigmaFrame(node_id="11:1", name="welcome", description="Welcome screen."),
        FigmaFrame(node_id="11:2", name="permissions", description="Camera access prompt."),
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
        flows=[("11:1", "11:2")],
        version="v1",
        last_modified="2026-03-31T00:00:00Z",
    )


def _make_entry() -> PageEntry:
    return PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/abc123/pages/onboarding.md",
        page_hash="deadbeef",
        last_refreshed_at="2026-03-31T00:00:00Z",
    )


def _write_md(tmp_path: Path) -> Path:
    page = _make_page()
    entry = _make_entry()
    md = scaffold_page(page, entry)
    md_path = tmp_path / "page.md"
    md_path.write_text(md)
    return md_path


def test_write_body_writes_new_body(tmp_path: Path) -> None:
    """INVARIANT: write-body replaces the body below frontmatter with new content."""
    md_path = _write_md(tmp_path)
    new_body = "# New Title\n\nThis is the new body written by the LLM.\n"

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body",
        str(md_path),
        "--body", new_body,
    ])
    assert result.exit_code == 0, result.output

    post = _frontmatter.loads(md_path.read_text())
    assert "This is the new body written by the LLM." in post.content


def test_bp6_write_body_preserves_frontmatter_byte_for_byte(tmp_path: Path) -> None:
    """BP-6: write-body preserves frontmatter byte-for-byte."""
    md_path = _write_md(tmp_path)
    original_md = md_path.read_text()

    # Extract original frontmatter block
    _, _, after_open = original_md.partition("---\n")
    original_fm_body, _, _ = after_open.partition("\n---")

    new_body = "Completely different body content.\n\nWith multiple paragraphs."
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body",
        str(md_path),
        "--body", new_body,
    ])
    assert result.exit_code == 0, result.output

    updated_md = md_path.read_text()
    _, _, after_open2 = updated_md.partition("---\n")
    updated_fm_body, _, _ = after_open2.partition("\n---")

    assert updated_fm_body == original_fm_body, (
        "BP-6 VIOLATED: write-body modified the frontmatter.\n"
        f"Expected:\n{original_fm_body}\n\nActual:\n{updated_fm_body}"
    )

    # Frontmatter must still parse correctly
    fm = parse_frontmatter(updated_md)
    assert fm is not None
    assert fm.file_key == "abc123"
    assert "11:1" in fm.frames
    assert [tuple(e) for e in fm.flows] == [("11:1", "11:2")]


def test_write_body_via_stdin(tmp_path: Path) -> None:
    """INVARIANT: write-body reads body from stdin when --body is not given."""
    md_path = _write_md(tmp_path)
    new_body = "Body from stdin.\n"

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body",
        str(md_path),
    ], input=new_body)
    assert result.exit_code == 0, result.output

    post = _frontmatter.loads(md_path.read_text())
    assert "Body from stdin." in post.content


def test_write_body_via_file(tmp_path: Path) -> None:
    """INVARIANT: write-body reads body from a file path when --body points to one."""
    md_path = _write_md(tmp_path)
    body_file = tmp_path / "new_body.md"
    body_file.write_text("# LLM Output\n\nBody loaded from file.\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body",
        str(md_path),
        "--body", str(body_file),
    ])
    assert result.exit_code == 0, result.output

    post = _frontmatter.loads(md_path.read_text())
    assert "Body loaded from file." in post.content


def test_write_body_fails_for_non_figmaclaw_file(tmp_path: Path) -> None:
    """INVARIANT: write-body fails for files without figmaclaw frontmatter."""
    md_path = tmp_path / "plain.md"
    md_path.write_text("# Just markdown\n\nNo frontmatter.\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body",
        str(md_path),
        "--body", "new body",
    ])
    assert result.exit_code != 0


def test_write_body_survives_repeated_calls(tmp_path: Path) -> None:
    """INVARIANT: frontmatter survives multiple write-body calls without degradation."""
    md_path = _write_md(tmp_path)
    original_md = md_path.read_text()
    _, _, after_open = original_md.partition("---\n")
    original_fm_body, _, _ = after_open.partition("\n---")

    runner = CliRunner()
    for i in range(5):
        result = runner.invoke(cli, [
            "--repo-dir", str(tmp_path),
            "write-body",
            str(md_path),
            "--body", f"Body version {i}.\n",
        ])
        assert result.exit_code == 0, result.output

    updated_md = md_path.read_text()
    _, _, after_open2 = updated_md.partition("---\n")
    updated_fm_body, _, _ = after_open2.partition("\n---")

    assert updated_fm_body == original_fm_body, "Frontmatter degraded after repeated write-body calls"
    assert "Body version 4." in updated_md
