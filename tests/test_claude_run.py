"""
Tests for figmaclaw.scripts.claude_run — the Claude Code CI launcher.

These tests verify the file-filtering and enrichment-detection logic that
decides which files get passed to Claude for enrichment. The actual Claude
invocation is never called — we test everything up to that boundary.

INVARIANT: claude_run.py must always be valid Python. A syntax error here
breaks the entire enrichment pipeline (24+ hours of silent CI failures).
"""
from __future__ import annotations

import py_compile
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from figmaclaw.scripts.claude_run import (
    MAX_FRAMES_PER_FILE,
    _build_prompt,
    _collect_files,
    _enrichment_info,
)


# ---------------------------------------------------------------------------
# META-TEST: syntax validity (would have caught the orphaned except block)
# ---------------------------------------------------------------------------


class TestSyntaxValidity:
    """claude_run.py must always compile. This is the canary test."""

    def test_module_compiles(self) -> None:
        """INVARIANT: the script must be valid Python — a syntax error here
        silently breaks the entire CI enrichment pipeline."""
        script = Path(__file__).parent.parent / "figmaclaw" / "scripts" / "claude_run.py"
        py_compile.compile(str(script), doraise=True)

    def test_stream_formatter_compiles(self) -> None:
        script = Path(__file__).parent.parent / "figmaclaw" / "scripts" / "stream_formatter.py"
        py_compile.compile(str(script), doraise=True)

    def test_all_scripts_compile(self) -> None:
        """Every .py file in figmaclaw/scripts/ must compile."""
        scripts_dir = Path(__file__).parent.parent / "figmaclaw" / "scripts"
        for py_file in scripts_dir.glob("*.py"):
            py_compile.compile(str(py_file), doraise=True)


# ---------------------------------------------------------------------------
# _enrichment_info — fast check for whether a file needs enrichment
# ---------------------------------------------------------------------------


class TestEnrichmentInfo:
    """_enrichment_info reads frontmatter to decide if a file needs enrichment."""

    def test_file_not_found_returns_false(self, tmp_path: Path) -> None:
        """Missing file → (False, 0). Never crash on missing files."""
        needs, count = _enrichment_info(tmp_path / "nonexistent.md")
        assert needs is False
        assert count == 0

    def test_file_without_enriched_hash_needs_enrichment(self, tmp_path: Path) -> None:
        """No enriched_hash → needs enrichment."""
        md = tmp_path / "page.md"
        md.write_text(textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            frames: ["1:1", "1:2"]
            ---

            # Page Title

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | (no description yet) |
            | Home   | `1:2`   | (no description yet) |
        """))
        needs, count = _enrichment_info(md)
        assert needs is True
        assert count == 2

    def test_file_with_enriched_hash_skipped(self, tmp_path: Path) -> None:
        """enriched_hash in frontmatter → already enriched, skip."""
        md = tmp_path / "page.md"
        md.write_text(textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            enriched_hash: "sha256:abcdef1234567890"
            enriched_at: "2026-04-01T00:00:00Z"
            ---

            # Page Title

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | Login screen with email/password form |
        """))
        needs, count = _enrichment_info(md)
        assert needs is False
        assert count == 0

    def test_counts_frame_rows_correctly(self, tmp_path: Path) -> None:
        """Frame count = table rows with backtick node IDs, excluding header/separator."""
        md = tmp_path / "page.md"
        md.write_text(textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            ---

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | desc |
            | Home   | `1:2`   | desc |
            | Profile | `1:3`  | desc |
        """))
        needs, count = _enrichment_info(md)
        assert needs is True
        assert count == 3

    def test_empty_file_needs_enrichment(self, tmp_path: Path) -> None:
        """Empty file → needs enrichment, 0 frames."""
        md = tmp_path / "page.md"
        md.write_text("")
        needs, count = _enrichment_info(md)
        assert needs is True
        assert count == 0

    def test_enriched_hash_in_body_not_frontmatter_still_needs_enrichment(
        self, tmp_path: Path
    ) -> None:
        """enriched_hash must be in frontmatter (before closing ---), not body."""
        md = tmp_path / "page.md"
        md.write_text(textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            ---

            Some body text mentioning enriched_hash: fake

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | desc |
        """))
        needs, count = _enrichment_info(md)
        assert needs is True
        assert count == 1

    def test_no_frontmatter_delimiter(self, tmp_path: Path) -> None:
        """File without --- delimiter → no frontmatter → needs enrichment."""
        md = tmp_path / "page.md"
        md.write_text("Just plain text, no frontmatter at all.")
        needs, count = _enrichment_info(md)
        assert needs is True
        assert count == 0

    def test_header_and_separator_rows_not_counted(self, tmp_path: Path) -> None:
        """Table header (Node ID) and separator (---) rows must not be counted as frames."""
        md = tmp_path / "page.md"
        md.write_text(textwrap.dedent("""\
            ---
            file_key: x
            ---

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | A | `1:1` | desc |
        """))
        needs, count = _enrichment_info(md)
        assert needs is True
        assert count == 1  # only the data row, not header or separator


# ---------------------------------------------------------------------------
# _collect_files — file discovery and filtering
# ---------------------------------------------------------------------------


class TestCollectFiles:
    """_collect_files finds, filters, and sorts files for enrichment."""

    def _make_page(self, path: Path, *, enriched: bool = False, frames: int = 2) -> None:
        """Create a minimal figma page .md file."""
        fm_lines = ["---", "file_key: abc", 'page_node_id: "0:1"']
        if enriched:
            fm_lines.append('enriched_hash: "sha256:abc"')
        fm_lines.append("---")
        fm_lines.append("")
        fm_lines.append("| Screen | Node ID | Description |")
        fm_lines.append("|--------|---------|-------------|")
        for i in range(frames):
            fm_lines.append(f"| Screen{i} | `1:{i}` | desc |")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(fm_lines))

    def test_single_file_target(self, tmp_path: Path) -> None:
        """Single file target → returns [target], regardless of filters."""
        md = tmp_path / "page.md"
        self._make_page(md)
        result = _collect_files(md, "**/*.md", changed_only=False)
        assert result == [md]

    def test_directory_glob(self, tmp_path: Path) -> None:
        """Directory target → glob matches."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "a.md")
        self._make_page(pages / "b.md")
        (tmp_path / "figma" / "other.txt").write_text("not a match")
        result = _collect_files(tmp_path / "figma", "**/*.md", changed_only=False)
        assert len(result) == 2

    def test_needs_enrichment_filters_enriched(self, tmp_path: Path) -> None:
        """needs_enrichment=True filters out files with enriched_hash."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "enriched.md", enriched=True)
        self._make_page(pages / "pending.md", enriched=False)
        result = _collect_files(
            tmp_path / "figma", "**/*.md", changed_only=False, needs_enrichment=True
        )
        assert len(result) == 1
        assert result[0].name == "pending.md"

    def test_needs_enrichment_skips_large_files(self, tmp_path: Path) -> None:
        """Files with more than MAX_FRAMES_PER_FILE frames are skipped."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "small.md", frames=5)
        self._make_page(pages / "huge.md", frames=MAX_FRAMES_PER_FILE + 1)
        result = _collect_files(
            tmp_path / "figma", "**/*.md", changed_only=False, needs_enrichment=True
        )
        assert len(result) == 1
        assert result[0].name == "small.md"

    def test_needs_enrichment_sorts_smallest_first(self, tmp_path: Path) -> None:
        """Files are sorted by frame count ascending — small files enriched first."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "big.md", frames=50)
        self._make_page(pages / "small.md", frames=3)
        self._make_page(pages / "medium.md", frames=20)
        result = _collect_files(
            tmp_path / "figma", "**/*.md", changed_only=False, needs_enrichment=True
        )
        assert [r.name for r in result] == ["small.md", "medium.md", "big.md"]

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty directory → empty list, no crash."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = _collect_files(empty, "**/*.md", changed_only=False)
        assert result == []


# ---------------------------------------------------------------------------
# _build_prompt — template substitution
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """_build_prompt fills template placeholders."""

    def test_fills_all_placeholders(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text("# Hello")
        template = "Process {file_path} named {filename} content={file_content} dir={target_dir}"
        result = _build_prompt(template, tmp_path, [md])
        assert str(md) in result
        assert "page.md" in result
        assert "# Hello" in result
        assert str(tmp_path) in result

    def test_file_list_placeholder(self, tmp_path: Path) -> None:
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("A")
        b.write_text("B")
        template = "files:\n{file_list}"
        result = _build_prompt(template, tmp_path, [a, b])
        assert f"- {a}" in result
        assert f"- {b}" in result

    def test_missing_file_gives_empty_content(self, tmp_path: Path) -> None:
        md = tmp_path / "nonexistent.md"
        template = "content=[{file_content}]"
        result = _build_prompt(template, tmp_path, [md])
        assert "content=[]" in result


# ---------------------------------------------------------------------------
# Integration: dry-run CLI
# ---------------------------------------------------------------------------


class TestDryRun:
    """The --dry-run flag prints files without invoking claude."""

    def test_dry_run_lists_files(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text("---\nfile_key: x\n---\n")
        result = subprocess.run(
            [
                "python", "-m", "figmaclaw.scripts.claude_run",
                str(md),
                "--prompt", "test {file_path}",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert str(md) in result.stdout

    def test_dry_run_no_files_exits_zero(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = subprocess.run(
            [
                "python", "-m", "figmaclaw.scripts.claude_run",
                str(empty),
                "--prompt", "test",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "No files found" in result.stderr
