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
from figmaclaw.main import cli
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


# ---------------------------------------------------------------------------
# write-body --section: surgical section replacement
# ---------------------------------------------------------------------------


_SECTION_TEST_MD = """\
---
file_key: abc
page_node_id: '1:1'
frames: ['11:1', '11:2', '21:1']
---

# File / Page

[Open in Figma](https://figma.com)

Page summary text.

## Auth (`10:1`)

Auth section intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | (no description yet) |
| Signup | `11:2` | (no description yet) |

## Dashboard (`20:1`)

Dashboard intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Home | `21:1` | (no description yet) |

## Screen flows

```mermaid
flowchart LR
    A["Login"] --> B["Home"]
```
"""


def test_write_section_replaces_only_target(tmp_path: Path) -> None:
    """INVARIANT: --section replaces only the specified section."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    new_auth = """\
## Auth (`10:1`)

Updated auth intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | A login screen |
| Signup | `11:2` | A signup screen |"""

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "10:1",
        "--body", new_auth,
    ])
    assert result.exit_code == 0, result.output

    updated = md_path.read_text()
    # Auth section was updated
    assert "Updated auth intro." in updated
    assert "A login screen" in updated
    # Dashboard section is untouched
    assert "Dashboard intro." in updated
    assert "| Home | `21:1` | (no description yet) |" in updated


def test_write_section_preserves_frontmatter(tmp_path: Path) -> None:
    """INVARIANT: --section preserves frontmatter byte-for-byte (BP-6)."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    original_fm = parse_frontmatter(_SECTION_TEST_MD)
    assert original_fm is not None

    new_section = "## Auth (`10:1`)\n\nNew intro.\n\n| Screen | Node ID | Description |\n|--------|---------|-------------|\n| Login | `11:1` | desc |"
    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "10:1",
        "--body", new_section,
    ])

    updated_fm = parse_frontmatter(md_path.read_text())
    assert updated_fm is not None
    assert updated_fm.file_key == original_fm.file_key
    assert updated_fm.frames == original_fm.frames


def test_write_section_preserves_page_summary(tmp_path: Path) -> None:
    """INVARIANT: page summary (text before first ##) is untouched."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    new_section = "## Auth (`10:1`)\n\nNew.\n\n| Screen | Node ID | Description |\n|--------|---------|-------------|\n| Login | `11:1` | desc |"
    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "10:1",
        "--body", new_section,
    ])

    updated = md_path.read_text()
    assert "Page summary text." in updated
    assert "[Open in Figma]" in updated


def test_write_section_preserves_screen_flows(tmp_path: Path) -> None:
    """INVARIANT: Screen flows mermaid block is untouched."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    new_section = "## Dashboard (`20:1`)\n\nUpdated.\n\n| Screen | Node ID | Description |\n|--------|---------|-------------|\n| Home | `21:1` | described |"
    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "20:1",
        "--body", new_section,
    ])

    updated = md_path.read_text()
    assert "## Screen flows" in updated
    assert "```mermaid" in updated
    assert 'A["Login"] --> B["Home"]' in updated


def test_write_section_last_section_before_screen_flows(tmp_path: Path) -> None:
    """INVARIANT: replacing the last section before Screen flows doesn't eat the mermaid."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    new_section = "## Dashboard (`20:1`)\n\nReplaced dashboard.\n\n| Screen | Node ID | Description |\n|--------|---------|-------------|\n| Home | `21:1` | new desc |"
    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "20:1",
        "--body", new_section,
    ])

    updated = md_path.read_text()
    assert "Replaced dashboard." in updated
    assert "## Screen flows" in updated


def test_write_section_intro_only(tmp_path: Path) -> None:
    """INVARIANT: --section --intro updates only the intro, table untouched."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "10:1",
        "--intro", "Updated auth intro via --intro flag.",
    ])
    assert result.exit_code == 0, result.output

    updated = md_path.read_text()
    assert "Updated auth intro via --intro flag." in updated
    # Frame table is untouched
    assert "| Login | `11:1` | (no description yet) |" in updated
    assert "| Signup | `11:2` | (no description yet) |" in updated
    # Other sections untouched
    assert "Dashboard intro." in updated
    # Mermaid untouched
    assert "```mermaid" in updated


def test_write_section_intro_replaces_existing(tmp_path: Path) -> None:
    """INVARIANT: --intro replaces existing intro, not appends."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    # First write
    runner = CliRunner()
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "10:1",
        "--intro", "First intro.",
    ])
    # Second write
    runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "10:1",
        "--intro", "Second intro.",
    ])

    updated = md_path.read_text()
    assert "Second intro." in updated
    assert "First intro." not in updated


def test_write_section_not_found(tmp_path: Path) -> None:
    """INVARIANT: --section with unknown node_id fails with UsageError."""
    md_path = tmp_path / "page.md"
    md_path.write_text(_SECTION_TEST_MD)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(tmp_path),
        "write-body", str(md_path),
        "--section", "99:99",
        "--body", "## Nope (`99:99`)\n\nwhatever",
    ])
    assert result.exit_code != 0
