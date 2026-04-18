"""
Tests for figmaclaw claude-run — the Claude Code CI enrichment command.

These tests verify the file-filtering and enrichment-detection logic that
decides which files get passed to Claude for enrichment. The actual Claude
invocation is never called — we test everything up to that boundary.

INVARIANT: all figmaclaw commands must be valid Python. A syntax error
breaks the entire enrichment pipeline (24+ hours of silent CI failures).
"""

from __future__ import annotations

import csv
import json
import py_compile
import textwrap
from pathlib import Path
from unittest.mock import Mock

from click.testing import CliRunner

from figmaclaw.commands import claude_run as claude_run_mod
from figmaclaw.commands.claude_run import (
    ENRICHMENT_LOG_SCHEMA_VERSION,
    build_prompt,
    collect_files,
    enrichment_info,
    needs_finalization,
    pending_sections,
)
from figmaclaw.main import cli

# ---------------------------------------------------------------------------
# META-TEST: syntax validity (would have caught the orphaned except block)
# ---------------------------------------------------------------------------


class TestSyntaxValidity:
    """All command modules must compile. This is the canary test."""

    def test_claude_run_compiles(self) -> None:
        """INVARIANT: the command module must be valid Python — a syntax error
        silently breaks the entire CI enrichment pipeline."""
        script = Path(__file__).parent.parent / "figmaclaw" / "commands" / "claude_run.py"
        py_compile.compile(str(script), doraise=True)

    def test_stream_format_compiles(self) -> None:
        script = Path(__file__).parent.parent / "figmaclaw" / "commands" / "stream_format.py"
        py_compile.compile(str(script), doraise=True)


# ---------------------------------------------------------------------------
# enrichment_info — fast check for whether a file needs enrichment
# ---------------------------------------------------------------------------


class TestEnrichmentInfo:
    """enrichment_info reads frontmatter to decide if a file needs enrichment."""

    def test_file_not_found_returns_false(self, tmp_path: Path) -> None:
        """Missing file → (False, 0). Never crash on missing files."""
        needs, count = enrichment_info(tmp_path / "nonexistent.md")
        assert needs is False
        assert count == 0

    def test_file_without_enriched_hash_needs_enrichment(self, tmp_path: Path) -> None:
        """No enriched_hash → needs enrichment."""
        md = tmp_path / "page.md"
        md.write_text(
            textwrap.dedent("""\
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
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is True
        assert count == 2

    def test_file_with_enriched_hash_skipped(self, tmp_path: Path) -> None:
        """enriched_hash in frontmatter → already enriched, skip."""
        md = tmp_path / "page.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            enriched_hash: "sha256:abcdef1234567890"
            enriched_at: "2026-04-01T00:00:00Z"
            enriched_schema_version: 1
            ---

            # Page Title

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | Login screen with email/password form |
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is False
        assert count == 0

    def test_enriched_hash_with_placeholders_still_needs_enrichment(self, tmp_path: Path) -> None:
        """Placeholder rows must override enriched_hash and force re-enrichment."""
        md = tmp_path / "page.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            enriched_hash: "sha256:abcdef1234567890"
            enriched_at: "2026-04-01T00:00:00Z"
            ---

            # Page Title

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | (no description yet) |
            | Home   | `1:2`   | (no description yet) |
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is True
        assert count == 2

    def test_enriched_hash_with_llm_marker_still_needs_enrichment(self, tmp_path: Path) -> None:
        """LLM marker rows must override enriched_hash and force re-enrichment."""
        md = tmp_path / "page.md"
        md.write_text(
            textwrap.dedent("""            ---
            file_key: abc123
            page_node_id: "0:1"
            enriched_hash: "sha256:abcdef1234567890"
            enriched_at: "2026-04-01T00:00:00Z"
            enriched_schema_version: 1
            ---

            # Page Title

            <!-- LLM: needs rewrite for stale prose -->

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | Login screen with email/password form |
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is True
        assert count == 1

    def test_counts_frame_rows_correctly(self, tmp_path: Path) -> None:
        """Frame count = table rows with backtick node IDs, excluding header/separator."""
        md = tmp_path / "page.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            ---

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | desc |
            | Home   | `1:2`   | desc |
            | Profile | `1:3`  | desc |
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is True
        assert count == 3

    def test_empty_file_skipped(self, tmp_path: Path) -> None:
        """Empty file → no figmaclaw frontmatter → skip."""
        md = tmp_path / "page.md"
        md.write_text("")
        needs, count = enrichment_info(md)
        assert needs is False

    def test_no_frontmatter_skipped(self, tmp_path: Path) -> None:
        """File without file_key → not a figmaclaw file → skip."""
        md = tmp_path / "page.md"
        md.write_text("# Just a README\nSome text\n")
        needs, count = enrichment_info(md)
        assert needs is False

    def test_enriched_hash_in_body_not_frontmatter_still_needs_enrichment(
        self, tmp_path: Path
    ) -> None:
        """enriched_hash must be in frontmatter (before closing ---), not body."""
        md = tmp_path / "page.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            ---

            Some body text mentioning enriched_hash: fake

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | desc |
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is True
        assert count == 1

    def test_no_frontmatter_delimiter(self, tmp_path: Path) -> None:
        """File without --- delimiter → no figmaclaw frontmatter → skip."""
        md = tmp_path / "page.md"
        md.write_text("Just plain text, no frontmatter at all.")
        needs, count = enrichment_info(md)
        assert needs is False

    def test_header_and_separator_rows_not_counted(self, tmp_path: Path) -> None:
        """Table header (Node ID) and separator (---) rows must not be counted as frames."""
        md = tmp_path / "page.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            file_key: x
            ---

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | A | `1:1` | desc |
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is True
        assert count == 1

    def test_census_file_is_never_enrichable(self, tmp_path: Path) -> None:
        """_census.md is an inventory artifact and must never enter enrichment."""
        md = tmp_path / "_census.md"
        md.write_text(
            textwrap.dedent("""\
            ---
            file_key: abc123
            ---

            | Component set | Key | Page | Updated |
            |---|---|---|---|
            | `Button` | `k1` | Components | 2026-04-15 |
        """)
        )
        needs, count = enrichment_info(md)
        assert needs is False
        assert count == 0


# ---------------------------------------------------------------------------
# collect_files — file discovery and filtering
# ---------------------------------------------------------------------------


class TestCollectFiles:
    """collect_files finds, filters, and sorts files for enrichment."""

    def _make_page(self, path: Path, *, enriched: bool = False, frames: int = 2) -> None:
        """Create a minimal figma page .md file."""
        fm_lines = ["---", "file_key: abc", 'page_node_id: "0:1"']
        if enriched:
            fm_lines.append('enriched_hash: "sha256:abc"')
            fm_lines.append("enriched_schema_version: 1")
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
        result = collect_files(md, "**/*.md", changed_only=False)
        assert result == [md]

    def test_single_file_target_census_is_skipped(self, tmp_path: Path) -> None:
        """Single-file census targets are ignored as non-enrichable artifacts."""
        census = tmp_path / "_census.md"
        census.write_text("---\nfile_key: abc123\n---\n")
        result = collect_files(census, "**/*.md", changed_only=False)
        assert result == []

    def test_directory_glob(self, tmp_path: Path) -> None:
        """Directory target → glob matches."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "a.md")
        self._make_page(pages / "b.md")
        (tmp_path / "figma" / "other.txt").write_text("not a match")
        result = collect_files(tmp_path / "figma", "**/*.md", changed_only=False)
        assert len(result) == 2

    def test_directory_glob_skips_census_even_without_needs_enrichment(
        self, tmp_path: Path
    ) -> None:
        """Census files are excluded during discovery even without needs_enrichment."""
        root = tmp_path / "figma" / "web-app-abc123"
        self._make_page(root / "pages" / "a.md")
        (root / "_census.md").write_text("---\nfile_key: abc123\n---\n")
        result = collect_files(tmp_path / "figma", "**/*.md", changed_only=False)
        assert [p.name for p in result] == ["a.md"]

    def test_needs_enrichment_filters_enriched(self, tmp_path: Path) -> None:
        """needs_enrichment=True filters out files with enriched_hash."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "enriched.md", enriched=True)
        self._make_page(pages / "pending.md", enriched=False)
        result = collect_files(
            tmp_path / "figma", "**/*.md", changed_only=False, needs_enrichment=True
        )
        assert len(result) == 1
        assert result[0].name == "pending.md"

    def test_needs_enrichment_keeps_enriched_file_when_placeholders_exist(
        self, tmp_path: Path
    ) -> None:
        """Enriched files with placeholders must remain in the enrichment queue."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "enriched.md", enriched=True)
        self._make_page(pages / "pending.md", enriched=False)
        text = (pages / "enriched.md").read_text()
        (pages / "enriched.md").write_text(
            text.replace("| Screen0 | `1:0` | desc |", "| Screen0 | `1:0` | (no description yet) |")
        )

        result = collect_files(
            tmp_path / "figma", "**/*.md", changed_only=False, needs_enrichment=True
        )
        assert sorted(p.name for p in result) == ["enriched.md", "pending.md"]

    def test_needs_enrichment_skips_files_above_max_frames(self, tmp_path: Path) -> None:
        """Files with more than max_frames are filtered out."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "small.md", frames=5)
        self._make_page(pages / "huge.md", frames=81)
        result = collect_files(
            tmp_path / "figma",
            "**/*.md",
            changed_only=False,
            needs_enrichment=True,
            max_frames=80,
        )
        assert len(result) == 1
        assert result[0].name == "small.md"

    def test_needs_enrichment_skips_files_below_min_frames(self, tmp_path: Path) -> None:
        """Files with fewer than min_frames are filtered out."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "small.md", frames=5)
        self._make_page(pages / "large.md", frames=100)
        result = collect_files(
            tmp_path / "figma",
            "**/*.md",
            changed_only=False,
            needs_enrichment=True,
            min_frames=81,
        )
        assert len(result) == 1
        assert result[0].name == "large.md"

    def test_needs_enrichment_sorts_smallest_first(self, tmp_path: Path) -> None:
        """Files are sorted by frame count ascending — small files enriched first."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "big.md", frames=50)
        self._make_page(pages / "small.md", frames=3)
        self._make_page(pages / "medium.md", frames=20)
        result = collect_files(
            tmp_path / "figma", "**/*.md", changed_only=False, needs_enrichment=True
        )
        assert [r.name for r in result] == ["small.md", "medium.md", "big.md"]

    def test_needs_enrichment_skips_census_files(self, tmp_path: Path) -> None:
        """Census markdown files must never be selected for enrichment."""
        figma_dir = tmp_path / "figma" / "design-system-abc123"
        pages = figma_dir / "pages"
        self._make_page(pages / "pending.md", enriched=False, frames=3)
        (figma_dir / "_census.md").write_text(
            textwrap.dedent("""\
            ---
            file_key: abc123
            ---

            | Component set | Key | Page | Updated |
            |---|---|---|---|
            | `Button` | `k1` | Components | 2026-04-15 |
        """)
        )
        result = collect_files(
            tmp_path / "figma", "**/*.md", changed_only=False, needs_enrichment=True
        )
        assert [r.name for r in result] == ["pending.md"]

    def test_empty_directory(self, tmp_path: Path) -> None:
        """Empty directory → empty list, no crash."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = collect_files(empty, "**/*.md", changed_only=False)
        assert result == []


# ---------------------------------------------------------------------------
# build_prompt — template substitution
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    """build_prompt fills template placeholders."""

    def test_fills_all_placeholders(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text("# Hello")
        template = "Process {file_path} named {filename} content={file_content} dir={target_dir}"
        result = build_prompt(template, tmp_path, [md])
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
        result = build_prompt(template, tmp_path, [a, b])
        assert f"- {a}" in result
        assert f"- {b}" in result

    def test_missing_file_gives_empty_content(self, tmp_path: Path) -> None:
        md = tmp_path / "nonexistent.md"
        template = "content=[{file_content}]"
        result = build_prompt(template, tmp_path, [md])
        assert "content=[]" in result


# ---------------------------------------------------------------------------
# CLI integration: dry-run and help
# ---------------------------------------------------------------------------


class TestCLI:
    """Click CLI integration tests."""

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["claude-run", "--help"])
        assert result.exit_code == 0
        assert "Launch claude" in result.output

    def test_dry_run_lists_files(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text("---\nfile_key: x\n---\n")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--prompt",
                "test {file_path}",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert str(md) in result.output

    def test_dry_run_no_files_exits_zero(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(empty),
                "--prompt",
                "test",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0

    def test_stream_format_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["stream-format", "--help"])
        assert result.exit_code == 0
        assert "stream-json" in result.output

    def test_section_mode_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["claude-run", "--help"])
        assert result.exit_code == 0
        assert "--section-mode" in result.output


# ---------------------------------------------------------------------------
# Section-mode helpers: pending_sections and needs_finalization
# ---------------------------------------------------------------------------

_LARGE_PAGE_MD = """\
---
file_key: abc
page_node_id: '1:1'
---

# File / Page

## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | (no description yet) |
| Signup | `11:2` | (no description yet) |

## Dashboard (`20:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Home | `21:1` | (no description yet) |
"""

_PARTIALLY_ENRICHED_MD = """\
---
file_key: abc
page_node_id: '1:1'
---

# File / Page

## Auth (`10:1`)

Auth intro.

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | A login screen |
| Signup | `11:2` | A signup screen |

## Dashboard (`20:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Home | `21:1` | (no description yet) |
"""

_FULLY_DESCRIBED_MD = """\
---
file_key: abc
page_node_id: '1:1'
---

# File / Page

## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | A login screen |

## Dashboard (`20:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Home | `21:1` | A dashboard |
"""

_FULLY_ENRICHED_MD = """\
---
file_key: abc
page_node_id: '1:1'
enriched_hash: deadbeef12345678
---

# File / Page

## Auth (`10:1`)

| Screen | Node ID | Description |
|--------|---------|-------------|
| Login | `11:1` | A login screen |
"""


class TestPendingSections:
    """pending_sections identifies sections with placeholder descriptions."""

    def test_all_pending(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_LARGE_PAGE_MD)
        sections = pending_sections(md)
        assert len(sections) == 2
        assert sections[0]["node_id"] == "10:1"
        assert sections[0]["pending_frames"] == 2
        assert sections[1]["node_id"] == "20:1"
        assert sections[1]["pending_frames"] == 1

    def test_partially_enriched(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_PARTIALLY_ENRICHED_MD)
        sections = pending_sections(md)
        assert len(sections) == 1
        assert sections[0]["node_id"] == "20:1"

    def test_fully_described(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_FULLY_DESCRIBED_MD)
        sections = pending_sections(md)
        assert len(sections) == 0

    def test_missing_file(self, tmp_path: Path) -> None:
        sections = pending_sections(tmp_path / "nonexistent.md")
        assert sections == []


class TestNeedsFinalization:
    """needs_finalization detects when all sections are done but page isn't marked enriched."""

    def test_true_when_described_but_no_enriched_hash(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_FULLY_DESCRIBED_MD)
        assert needs_finalization(md) is True

    def test_false_when_still_pending(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_PARTIALLY_ENRICHED_MD)
        assert needs_finalization(md) is False

    def test_false_when_already_enriched(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text(_FULLY_ENRICHED_MD)
        assert needs_finalization(md) is False

    def test_false_for_missing_file(self, tmp_path: Path) -> None:
        assert needs_finalization(tmp_path / "nonexistent.md") is False


class TestPendingSectionsStuckDetection:
    """Verify pending_sections detects undescribable frames."""

    def test_pending_remains_same_after_update(self, tmp_path: Path) -> None:
        """If write-descriptions can't update a row (wrong node_id format etc),
        pending_sections returns the same count — triggers stuck detection."""
        md = tmp_path / "page.md"
        md.write_text(_LARGE_PAGE_MD)
        sections_before = pending_sections(md)
        assert len(sections_before) > 0
        # Simulate a "successful" batch that didn't change anything
        sections_after = pending_sections(md)
        assert sections_before == sections_after  # same pending = stuck


class TestPendingSectionsEmptyNameRegression:
    """figmaclaw#25: empty-name sections must not silently hide their pending frames."""

    def test_legacy_empty_name_section_frames_counted(self, tmp_path: Path) -> None:
        """A file written before the normalization fix has ``##  (`id`)``
        headings — two spaces, no name. pending_sections MUST enumerate the
        frames beneath such a section. Before figmaclaw#25 was fixed this
        silently returned an empty list and the file stalled forever.
        """
        md = tmp_path / "page.md"
        md.write_text(
            "---\n"
            "file_key: abc\n"
            "page_node_id: '1:1'\n"
            "---\n"
            "\n"
            "# File / Page\n"
            "\n"
            "##  (`20:1`)\n"  # empty name — the figmaclaw#25 shape
            "\n"
            "| Screen | Node ID | Description |\n"
            "|--------|---------|-------------|\n"
            "| Frame A | `21:1` | (no description yet) |\n"
            "| Frame B | `21:2` | (no description yet) |\n"
            "| Frame C | `21:3` | (no description yet) |\n"
        )
        sections = pending_sections(md)
        assert len(sections) == 1, "Empty-name section was dropped — the bug is back"
        assert sections[0]["node_id"] == "20:1"
        assert sections[0]["pending_frames"] == 3


class TestBuildPromptSectionPlaceholders:
    """build_prompt fills section-mode placeholders."""

    def test_section_placeholders(self, tmp_path: Path) -> None:
        md = tmp_path / "page.md"
        md.write_text("# content")
        template = "Enrich {section_node_id} ({section_name}) in {file_path}"
        result = build_prompt(
            template,
            tmp_path,
            [md],
            section_node_id="10:1",
            section_name="Auth",
        )
        assert "10:1" in result
        assert "Auth" in result
        assert str(md) in result


class TestClaudeRunExecutionBranches:
    """Execution-path regressions not covered by selection-only tests."""

    @staticmethod
    def _write_placeholder_page(path: Path) -> None:
        path.write_text(
            textwrap.dedent("""\
            ---
            file_key: abc123
            page_node_id: "0:1"
            ---

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | (no description yet) |
        """)
        )

    @staticmethod
    def _budget_decision(should_start: bool, reason: str) -> claude_run_mod.BudgetDecision:
        return claude_run_mod.BudgetDecision(
            should_start=should_start,
            reason=reason,
            predicted_seconds=1.0,
            remaining_seconds=1000.0,
            per_frame_estimate=1.0,
            history_used=0,
        )

    def test_budget_exhaustion_stops_before_claude_call(self, tmp_path: Path, monkeypatch) -> None:
        md = tmp_path / "page.md"
        self._write_placeholder_page(md)

        monkeypatch.setattr(
            claude_run_mod,
            "decide_next_batch",
            lambda **_: self._budget_decision(False, "[budget] stop before first batch"),
        )
        run_mock = Mock(side_effect=AssertionError("claude should not run when budget stops"))
        monkeypatch.setattr(claude_run_mod, "_run_claude", run_mock)
        monkeypatch.setattr(claude_run_mod, "count_commits_since", lambda *_args, **_kwargs: 0)
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--prompt",
                "noop {file_path}",
            ],
        )

        assert result.exit_code == 0
        assert "[budget] stop before first batch" in result.output
        assert run_mock.call_count == 0

    def test_section_mode_phantom_selection_is_red(self, tmp_path: Path, monkeypatch) -> None:
        md = tmp_path / "page.md"
        self._write_placeholder_page(md)

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: [])
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_is_schema_upgrade_only_candidate", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_is_llm_marker_only_candidate", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_classify_no_work_candidate", lambda _p, **_kw: "phantom")
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--prompt",
                "noop {file_path}",
                "--section-mode",
            ],
        )

        assert result.exit_code == 2
        assert "PHANTOM SELECTION" in result.output
        assert "Verdict (row 5)" in result.output

    def test_section_mode_llm_marker_only_candidate_is_skipped_not_phantom(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        md = tmp_path / "marker-only.md"
        md.write_text(
            textwrap.dedent(
                """                ---
                file_key: abc123
                page_node_id: "0:1"
                enriched_hash: deadbeefcafebabe
                enriched_schema_version: 1
                ---

                <!-- LLM: rewrite this section -->

                | Screen | Node ID | Description |
                |--------|---------|-------------|
                | Login | `1:1` | already described |
                """
            )
        )

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: [])
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_is_schema_upgrade_only_candidate", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_is_llm_marker_only_candidate", lambda _p, **_kw: True)
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )
        run_mock = Mock()
        monkeypatch.setattr(claude_run_mod, "_run_claude", run_mock)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--section-mode",
            ],
        )

        assert result.exit_code == 0
        assert "skip (LLM-marker-only candidate)" in result.output
        assert "PHANTOM SELECTION" not in result.output
        assert run_mock.call_count == 0

    def test_section_mode_malformed_frontmatter_fails_closed_not_dispatch_crash(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        md = tmp_path / "malformed.md"
        md.write_text(
            textwrap.dedent(
                """                ---
                file_key: abc123
                page_node_id: "0:1"
                enriched_hash: deadbeefcafebabe
                enriched_schema_version: bad
                ---

                | Screen | Node ID | Description |
                |--------|---------|-------------|
                | Login | `1:1` | already described |
                """
            )
        )

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: [])
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )
        run_mock = Mock()
        monkeypatch.setattr(claude_run_mod, "_run_claude", run_mock)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--section-mode",
            ],
        )

        assert result.exit_code == 2
        assert "MALFORMED FRONTMATTER" in result.output
        assert "Verdict (row 5)" in result.output
        assert "RED (dispatch crash)" not in result.output
        assert run_mock.call_count == 0

    def test_section_mode_schema_only_candidate_is_skipped_not_phantom(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        md = tmp_path / "schema-only.md"
        md.write_text(
            textwrap.dedent(
                """                ---
                file_key: abc123
                page_node_id: "0:1"
                enriched_hash: deadbeefcafebabe
                enriched_schema_version: 0
                ---

                | Screen | Node ID | Description |
                |--------|---------|-------------|
                | Login | `1:1` | already described |
                """
            )
        )

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: [])
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_is_schema_upgrade_only_candidate", lambda _p, **_kw: True)
        monkeypatch.setattr(claude_run_mod, "_is_llm_marker_only_candidate", lambda _p, **_kw: False)
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )
        run_mock = Mock()
        monkeypatch.setattr(claude_run_mod, "_run_claude", run_mock)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--section-mode",
            ],
        )

        assert result.exit_code == 0
        assert "skip (schema-only candidate)" in result.output
        assert "PHANTOM SELECTION" not in result.output
        assert run_mock.call_count == 0

    def test_section_mode_stuck_detection_breaks_on_first_no_progress(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        md = tmp_path / "page.md"
        self._write_placeholder_page(md)

        sections = [{"node_id": "10:1", "name": "Auth", "pending_frames": 2}]
        pending = [sections, sections]

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: pending.pop(0))
        monkeypatch.setattr(claude_run_mod, "pending_frame_node_ids", lambda _p, **_kw: {"11:1", "11:2"})
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
        monkeypatch.setattr(
            claude_run_mod,
            "decide_next_batch",
            lambda **_: self._budget_decision(True, "[budget] go"),
        )
        run_mock = Mock(return_value=claude_run_mod.ClaudeResult(exit_code=0))
        monkeypatch.setattr(
            claude_run_mod,
            "_run_claude",
            run_mock,
        )
        monkeypatch.setattr(claude_run_mod, "count_commits_since", lambda *_args, **_kwargs: 1)
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--prompt",
                "noop {file_path}",
                "--section-mode",
            ],
        )

        assert result.exit_code == 0
        assert "NO-PROGRESS" in result.output
        assert run_mock.call_count == 1
        assert "Verdict (row 2)" in result.output

    def test_section_mode_phantom_selection_stops_run_immediately(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        page_a = tmp_path / "a.md"
        page_b = tmp_path / "b.md"
        self._write_placeholder_page(page_a)
        self._write_placeholder_page(page_b)

        monkeypatch.setattr(
            claude_run_mod, "collect_files", lambda *args, **kwargs: [page_a, page_b]
        )
        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: [])
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_is_schema_upgrade_only_candidate", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_is_llm_marker_only_candidate", lambda _p, **_kw: False)
        monkeypatch.setattr(claude_run_mod, "_classify_no_work_candidate", lambda _p, **_kw: "phantom")
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )
        run_mock = Mock()
        monkeypatch.setattr(claude_run_mod, "_run_claude", run_mock)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(page_a),
                "--section-mode",
            ],
        )

        assert result.exit_code == 2
        assert "PHANTOM SELECTION" in result.output
        assert "[2/2]" not in result.output
        assert run_mock.call_count == 0

    def test_dispatch_crash_is_always_red(self, tmp_path: Path, monkeypatch) -> None:
        md = tmp_path / "page.md"
        self._write_placeholder_page(md)

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 1))
        monkeypatch.setattr(
            claude_run_mod,
            "decide_next_batch",
            lambda **_: self._budget_decision(True, "[budget] go"),
        )
        monkeypatch.setattr(
            claude_run_mod,
            "_run_claude",
            lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        monkeypatch.setattr(claude_run_mod, "count_commits_since", lambda *_args, **_kwargs: 0)
        monkeypatch.setattr(
            claude_run_mod.subprocess,
            "run",
            lambda *args, **kwargs: Mock(stdout="", returncode=0),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "claude-run",
                str(md),
                "--prompt",
                "noop {file_path}",
            ],
        )

        assert "CRASH in dispatch loop: RuntimeError: boom" in result.output
        assert result.exit_code == 2


def test_collect_files_matches_inspect_enrich_must_schema_upgrade(tmp_path: Path) -> None:
    """Selector and inspect must agree when schema requires re-enrichment.

    Repro for figmaclaw issue #111:
    - inspect marks file as needs_enrichment=True via ENRICH MUST
    - collect_files(..., needs_enrichment=True) currently drops it
    """
    page = tmp_path / "figma" / "pages" / "schema-stale.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        textwrap.dedent(
            """\
            ---
            file_key: abc123
            page_node_id: "0:1"
            frames: ["1:1"]
            enriched_hash: deadbeefcafebabe
            enriched_schema_version: 0
            ---

            # File / Page

            ## Auth (`10:1`)

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login | `1:1` | A described frame |
            """
        )
    )

    runner = CliRunner()
    inspect_res = runner.invoke(
        cli,
        ["--repo-dir", str(tmp_path), "inspect", str(page), "--json"],
        catch_exceptions=False,
    )
    assert inspect_res.exit_code == 0
    inspect_data = json.loads(inspect_res.output)
    assert inspect_data["needs_enrichment"] is True
    assert inspect_data["enrichment_must_update"] is True

    selected = collect_files(
        tmp_path / "figma",
        "**/*.md",
        changed_only=False,
        needs_enrichment=True,
    )
    assert selected == [page]


def test_collect_files_matches_inspect_llm_marker_signal(tmp_path: Path) -> None:
    """Selector and inspect must agree on <!-- LLM: ... --> enrichment signal."""
    page = tmp_path / "figma" / "pages" / "llm-marker.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        textwrap.dedent(
            """            ---
            file_key: abc123
            page_node_id: "0:1"
            frames: ["1:1"]
            ---

            # File / Page

            <!-- LLM: rewrite section intro to reflect latest frame changes -->

            ## Auth (`10:1`)

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login | `1:1` | A described frame |
            """
        )
    )

    runner = CliRunner()
    inspect_res = runner.invoke(
        cli,
        ["--repo-dir", str(tmp_path), "inspect", str(page), "--json"],
        catch_exceptions=False,
    )
    assert inspect_res.exit_code == 0
    inspect_data = json.loads(inspect_res.output)
    assert inspect_data["needs_enrichment"] is True

    selected = collect_files(
        tmp_path / "figma",
        "**/*.md",
        changed_only=False,
        needs_enrichment=True,
    )
    assert selected == [page]


def test_collect_files_migrates_missing_schema_version_and_matches_inspect(tmp_path: Path) -> None:
    """Legacy enriched files without explicit schema version are migrated and selected.

    Invariant:
    - collect_files(... needs_enrichment=True) migrates missing enriched_schema_version
      to explicit 0 and agrees with inspect ENRICH MUST.
    """
    page = tmp_path / "figma" / "pages" / "legacy-no-schema.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        textwrap.dedent(
            """\
            ---
            file_key: abc123
            page_node_id: "0:1"
            frames: ["1:1"]
            enriched_hash: deadbeefcafebabe
            ---

            # File / Page

            ## Auth (`10:1`)

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login | `1:1` | A described frame |
            """
        )
    )

    selected = collect_files(
        tmp_path / "figma",
        "**/*.md",
        changed_only=False,
        needs_enrichment=True,
    )
    assert selected == [page]

    # Migration persisted explicitly.
    migrated_text = page.read_text()
    assert "enriched_schema_version: 0" in migrated_text

    runner = CliRunner()
    inspect_res = runner.invoke(
        cli,
        ["--repo-dir", str(tmp_path), "inspect", str(page), "--json"],
        catch_exceptions=False,
    )
    assert inspect_res.exit_code == 0
    inspect_data = json.loads(inspect_res.output)
    assert inspect_data["needs_enrichment"] is True
    assert inspect_data["enrichment_must_update"] is True


def test_enrichment_info_treats_no_screenshot_available_as_unresolved(tmp_path: Path) -> None:
    """Rows marked (no screenshot available) remain pending and retryable."""
    md = tmp_path / "page.md"
    md.write_text(
        textwrap.dedent(
            """\
            ---
            file_key: abc123
            page_node_id: "0:1"
            enriched_hash: "sha256:abcdef1234567890"
            enriched_at: "2026-04-01T00:00:00Z"
            enriched_schema_version: 1
            ---

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | (no screenshot available) |
            """
        )
    )
    needs, count = enrichment_info(md)
    assert needs is True
    assert count == 1


def test_enrichment_info_treats_screenshot_unavailable_as_unresolved(tmp_path: Path) -> None:
    """Rows marked (screenshot unavailable) remain pending and retryable."""
    md = tmp_path / "page.md"
    md.write_text(
        textwrap.dedent(
            """\
            ---
            file_key: abc123
            page_node_id: "0:1"
            enriched_hash: "sha256:abcdef1234567890"
            enriched_at: "2026-04-01T00:00:00Z"
            enriched_schema_version: 1
            ---

            | Screen | Node ID | Description |
            |--------|---------|-------------|
            | Login  | `1:1`   | (screenshot unavailable) |
            """
        )
    )
    needs, count = enrichment_info(md)
    assert needs is True
    assert count == 1


def test_pending_sections_counts_unavailable_rows_as_pending(tmp_path: Path) -> None:
    """pending_sections counts screenshot-unavailable rows as pending work."""
    md = tmp_path / "page.md"
    md.write_text(
        "---\n"
        "file_key: abc\n"
        "page_node_id: '1:1'\n"
        "---\n\n"
        "## Auth (`10:1`)\n\n"
        "| Screen | Node ID | Description |\n"
        "|--------|---------|-------------|\n"
        "| Login | `11:1` | (screenshot unavailable) |\n"
        "| Signup | `11:2` | (no screenshot available) |\n"
    )
    sections = pending_sections(md)
    assert len(sections) == 1
    assert sections[0]["pending_frames"] == 2


def test_needs_finalization_false_when_unavailable_rows_remain(tmp_path: Path) -> None:
    """Finalization must not run while unavailable screenshot markers remain."""
    md = tmp_path / "page.md"
    md.write_text(
        "---\n"
        "file_key: abc\n"
        "page_node_id: '1:1'\n"
        "---\n\n"
        "## Auth (`10:1`)\n\n"
        "| Screen | Node ID | Description |\n"
        "|--------|---------|-------------|\n"
        "| Login | `11:1` | (screenshot unavailable) |\n"
    )
    assert needs_finalization(md) is False


class TestEnrichmentLogSchemaAndIdempotency:
    """enrichment-log.csv is schema-stable, append-safe, and idempotent."""

    def test_log_writes_schema_header_and_event_id(self, tmp_path: Path) -> None:
        md = tmp_path / "figma" / "pages" / "a.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("---\nfile_key: a\n---\n")

        claude_run_mod._log_enrichment(
            tmp_path,
            md,
            "batch",
            12,
            31.0,
            True,
            section_name="Auth",
            claude=claude_run_mod.ClaudeResult(
                turns=3,
                cost_usd=0.1234,
                duration_ms=31000,
                stop_reason="end_turn",
            ),
        )

        log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
        with log_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1
        row = rows[0]
        assert row["schema_version"] == str(ENRICHMENT_LOG_SCHEMA_VERSION)
        assert row["event_id"] != ""
        assert row["mode"] == "batch"
        assert row["frames"] == "12"
        assert row["duration_s"] == "31"

    def test_log_is_idempotent_for_duplicate_event(self, tmp_path: Path) -> None:
        md = tmp_path / "figma" / "pages" / "a.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("---\nfile_key: a\n---\n")

        args = (tmp_path, md, "whole-page", 20, 42.0, True)
        kwargs = {"section_name": "", "claude": claude_run_mod.ClaudeResult(exit_code=0)}
        claude_run_mod._log_enrichment(*args, **kwargs)
        claude_run_mod._log_enrichment(*args, **kwargs)

        log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
        with log_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1

    def test_log_migrates_legacy_header_to_schema_versioned(self, tmp_path: Path) -> None:
        log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "timestamp,file,mode,frames,duration_s,success,section,"
            "turns,cost_usd,claude_duration_ms,stop_reason\n"
            "2026-04-15T00:00:00+00:00,figma/a.md,batch,10,50,True,Auth,2,0.1111,50000,end_turn\n",
            encoding="utf-8",
        )

        md = tmp_path / "figma" / "pages" / "a.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("---\nfile_key: a\n---\n")
        claude_run_mod._log_enrichment(tmp_path, md, "batch", 12, 31.0, True)

        with log_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert reader.fieldnames is not None
            assert "schema_version" in reader.fieldnames
            assert "event_id" in reader.fieldnames
        assert len(rows) == 2
        assert rows[0]["schema_version"] in {"0", str(ENRICHMENT_LOG_SCHEMA_VERSION)}
        assert rows[0]["event_id"] != ""
        assert rows[1]["schema_version"] == str(ENRICHMENT_LOG_SCHEMA_VERSION)

    def test_log_csv_escaping_preserves_commas_and_newlines(self, tmp_path: Path) -> None:
        md = tmp_path / "figma" / "pages" / "a.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("---\nfile_key: a\n---\n")

        section = "Auth, Login\nFlow"
        stop_reason = "tool_error, retry\nneeded"
        claude_run_mod._log_enrichment(
            tmp_path,
            md,
            "batch",
            5,
            19.0,
            False,
            section_name=section,
            claude=claude_run_mod.ClaudeResult(stop_reason=stop_reason),
        )

        log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
        with log_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        assert len(rows) == 1
        assert rows[0]["section"] == section
        assert rows[0]["stop_reason"] == stop_reason


def test_log_keeps_distinct_runs_with_identical_payload(tmp_path: Path) -> None:
    """INVARIANT: dedupe is run-scoped, not global across different runs."""
    md = tmp_path / "figma" / "pages" / "a.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nfile_key: a\n---\n")

    claude_run_mod._log_enrichment(
        tmp_path,
        md,
        "whole-page",
        20,
        42.0,
        True,
        run_id="run-a",
    )
    claude_run_mod._log_enrichment(
        tmp_path,
        md,
        "whole-page",
        20,
        42.0,
        True,
        run_id="run-b",
    )

    log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
    with log_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2


def test_log_migrates_minimal_legacy_header_without_optional_columns(tmp_path: Path) -> None:
    """7-column legacy logs should auto-migrate to canonical schema-v1."""
    log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "timestamp,file,mode,frames,duration_s,success,section\n"
        "2026-04-15T00:00:00+00:00,figma/a.md,batch,10,50,True,Auth\n",
        encoding="utf-8",
    )

    md = tmp_path / "figma" / "pages" / "a.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nfile_key: a\n---\n")

    claude_run_mod._log_enrichment(tmp_path, md, "batch", 12, 31.0, True, run_id="run-x")

    with log_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert reader.fieldnames is not None
        assert tuple(reader.fieldnames) == claude_run_mod.ENRICHMENT_LOG_FIELDS
    assert len(rows) == 2
    assert rows[0]["schema_version"] in {"0", str(ENRICHMENT_LOG_SCHEMA_VERSION)}
    assert rows[0]["event_id"] != ""


def test_log_migrates_real_world_linear_git_legacy_fixture(tmp_path: Path) -> None:
    """Real legacy header fixture from linear-git must auto-migrate and append."""
    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "enrichment_log_headers"
        / "linear_git_minimal_legacy_2026-04-03.csv"
    )
    log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

    md = tmp_path / "figma" / "pages" / "a.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nfile_key: a\n---\n")

    claude_run_mod._log_enrichment(tmp_path, md, "batch", 12, 31.0, True, run_id="run-x")

    with log_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert reader.fieldnames is not None
    assert tuple(reader.fieldnames) == claude_run_mod.ENRICHMENT_LOG_FIELDS
    assert len(rows) == 5
    assert rows[-1]["schema_version"] == str(ENRICHMENT_LOG_SCHEMA_VERSION)
    assert rows[-1]["event_id"] != ""


def test_log_unknown_header_self_heals_by_archiving_and_starting_fresh(
    tmp_path: Path,
) -> None:
    """Unsupported header must not become a silent-drop terminal state.

    Contract (figmaclaw#121 anti-loop policy #5): if schema migration is
    possible, auto-heal; if not, archive the prior file with a timestamp
    and start a fresh schema-v1 log. Never leave the writer in a "warn
    and skip forever" loop where every run loses its data.
    """
    log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("foo,bar,baz\n1,2,3\n", encoding="utf-8")

    md = tmp_path / "figma" / "pages" / "a.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nfile_key: a\n---\n")

    claude_run_mod._log_enrichment(tmp_path, md, "batch", 1, 1.0, True, run_id="run-x")

    # 1. A loud error line is emitted so humans can notice the archive event
    err = tmp_path / claude_run_mod.ENRICHMENT_LOG_ERROR
    assert err.exists()
    err_text = err.read_text(encoding="utf-8")
    assert "unrecognized enrichment log schema" in err_text
    assert "archived prior log" in err_text

    # 2. The prior log was archived with a .bak. prefix (not deleted)
    log_dir = log_path.parent
    backups = list(log_dir.glob(f"{log_path.stem}.bak.*{log_path.suffix}"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "foo,bar,baz\n1,2,3\n"

    # 3. A fresh schema-v1 log replaced it, and the caller's row landed
    with log_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert tuple(reader.fieldnames or ()) == claude_run_mod.ENRICHMENT_LOG_FIELDS
    assert len(rows) == 1
    assert rows[0]["mode"] == "batch"
    assert rows[0]["frames"] == "1"


def test_log_unknown_header_second_run_does_not_warn_again(tmp_path: Path) -> None:
    """After self-healing on run N, run N+1 must NOT re-emit the error.

    This is the cross-run idempotency shape from CLAUDE.md policy #1:
    a fix-once event should not be a fix-every-run event. If the warn
    recurred forever we'd still be silently losing signal.
    """
    log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("foo,bar,baz\n1,2,3\n", encoding="utf-8")

    md = tmp_path / "figma" / "pages" / "a.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nfile_key: a\n---\n")

    # Run 1 — archives + appends
    claude_run_mod._log_enrichment(tmp_path, md, "batch", 1, 1.0, True, run_id="run-1")
    err = tmp_path / claude_run_mod.ENRICHMENT_LOG_ERROR
    err_len_after_run1 = len(err.read_text(encoding="utf-8"))

    # Run 2 — clean log, should append without any new error line
    claude_run_mod._log_enrichment(tmp_path, md, "batch", 2, 2.0, True, run_id="run-2")

    assert len(err.read_text(encoding="utf-8")) == err_len_after_run1

    with log_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 2


def test_log_malformed_legacy_csv_is_observable_and_does_not_crash(tmp_path: Path) -> None:
    """Malformed legacy CSV should degrade safely (warn + skip), not raise."""
    log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "timestamp,file,mode,frames,duration_s,success,section,turns,cost_usd,claude_duration_ms,stop_reason\n"
        '2026-04-15T00:00:00+00:00,figma/a.md,batch,10,50,True,"broken,2,0.1111,50000,end_turn\n',
        encoding="utf-8",
    )

    md = tmp_path / "figma" / "pages" / "a.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nfile_key: a\n---\n")

    claude_run_mod._log_enrichment(tmp_path, md, "batch", 2, 2.0, True, run_id="run-x")

    err = tmp_path / claude_run_mod.ENRICHMENT_LOG_ERROR
    if err.exists():
        assert "failed reading rows" in err.read_text(encoding="utf-8")
    else:
        # Some malformed rows are still parseable by csv.DictReader; ensure write path survives.
        assert "schema_version,event_id" in log_path.read_text(encoding="utf-8")


def test_log_normalizes_header_with_extra_columns(tmp_path: Path) -> None:
    """Headers that include required columns + extras are normalized to canonical schema."""
    log_path = tmp_path / claude_run_mod.ENRICHMENT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "timestamp,file,mode,frames,duration_s,success,section,turns,cost_usd,claude_duration_ms,stop_reason,extra\n"
        "2026-04-15T00:00:00+00:00,figma/a.md,batch,10,50,True,Auth,2,0.1111,50000,end_turn,ignored\n",
        encoding="utf-8",
    )

    md = tmp_path / "figma" / "pages" / "a.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nfile_key: a\n---\n")

    claude_run_mod._log_enrichment(tmp_path, md, "batch", 12, 31.0, True, run_id="run-x")

    with log_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert reader.fieldnames is not None
        assert tuple(reader.fieldnames) == claude_run_mod.ENRICHMENT_LOG_FIELDS
    assert len(rows) == 2
