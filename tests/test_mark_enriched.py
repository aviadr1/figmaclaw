"""Tests for commands/mark_enriched.py.

INVARIANTS:
- mark-enriched writes enriched_schema_version = CURRENT_ENRICHMENT_SCHEMA_VERSION to frontmatter
- mark-enriched preserves raw_frames written by the pull pass (was a bug: previously dropped)
- mark-enriched preserves component_set_keys written by the pull pass (same bug)
- mark-enriched preserves the LLM body byte-for-byte
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from figmaclaw.figma_frontmatter import (
    CURRENT_ENRICHMENT_SCHEMA_VERSION,
)
from figmaclaw.figma_models import FigmaFrame, FigmaPage, FigmaSection
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry
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


def _make_entry(page_hash: str = "deadbeef12345678") -> PageEntry:
    return PageEntry(
        page_name="Onboarding",
        page_slug="onboarding",
        md_path="figma/web-app/pages/onboarding-7741-45837.md",
        page_hash=page_hash,
        last_refreshed_at="2026-03-31T00:00:00Z",
        frame_hashes={"11:1": "aabbccdd", "11:2": "eeff0011"},
    )


def _setup(tmp_path: Path, extra_frontmatter: str = "") -> Path:
    """Write a scaffold .md and populate the manifest. Returns the .md path."""
    page = _make_page()
    entry = _make_entry()

    md_dir = tmp_path / "figma" / "web-app" / "pages"
    md_dir.mkdir(parents=True)
    md_path = md_dir / "onboarding-7741-45837.md"

    scaffold_text = scaffold_page(page, entry)
    if extra_frontmatter:
        # Inject extra frontmatter fields before the closing ---
        scaffold_text = scaffold_text.replace("\n---\n", f"\n{extra_frontmatter}\n---\n", 1)
    md_path.write_text(scaffold_text)

    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file("abc123", "Web App")
    state.set_page_entry("abc123", "7741:45837", entry)
    state.save()

    return md_path


def test_mark_enriched_writes_enriched_schema_version(tmp_path: Path) -> None:
    """INVARIANT: mark-enriched writes enriched_schema_version = CURRENT to frontmatter."""
    md_path = _setup(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "mark-enriched",
            str(md_path),
        ],
    )

    assert result.exit_code == 0, result.output
    from figmaclaw.figma_parse import parse_frontmatter

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert fm.enriched_schema_version == CURRENT_ENRICHMENT_SCHEMA_VERSION


def test_mark_enriched_preserves_raw_frames_from_pull_pass(tmp_path: Path) -> None:
    """INVARIANT: raw_frames written by pull pass are NOT dropped by mark-enriched."""
    raw_frames_yaml = "raw_frames: {'11:1': {raw: 2, ds: [AvatarV2, ButtonV2]}}"
    md_path = _setup(tmp_path, extra_frontmatter=raw_frames_yaml)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "mark-enriched",
            str(md_path),
        ],
    )

    assert result.exit_code == 0, result.output
    from figmaclaw.figma_parse import parse_frontmatter

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert "11:1" in fm.raw_frames
    assert fm.raw_frames["11:1"].raw == 2
    assert fm.raw_frames["11:1"].ds == ["AvatarV2", "ButtonV2"]


def test_mark_enriched_preserves_component_set_keys_from_pull_pass(tmp_path: Path) -> None:
    """INVARIANT: component_set_keys written by pull pass are NOT dropped by mark-enriched."""
    csk_yaml = "component_set_keys: {ButtonV2: abc123key, AvatarV2: def456key}"
    md_path = _setup(tmp_path, extra_frontmatter=csk_yaml)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "mark-enriched",
            str(md_path),
        ],
    )

    assert result.exit_code == 0, result.output
    from figmaclaw.figma_parse import parse_frontmatter

    fm = parse_frontmatter(md_path.read_text())
    assert fm is not None
    assert fm.component_set_keys == {"ButtonV2": "abc123key", "AvatarV2": "def456key"}


def test_mark_enriched_preserves_llm_body(tmp_path: Path) -> None:
    """INVARIANT: mark-enriched never touches the LLM body — only frontmatter changes."""
    md_path = _setup(tmp_path)
    original_text = md_path.read_text()
    body_before = original_text.split("---\n", 2)[-1]

    runner = CliRunner()
    runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "mark-enriched",
            str(md_path),
        ],
    )

    body_after = md_path.read_text().split("---\n", 2)[-1]
    assert body_before == body_after
