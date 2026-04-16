"""
Tests for figmaclaw claude-run — the Claude Code CI enrichment command.

These tests verify the file-filtering and enrichment-detection logic that
decides which files get passed to Claude for enrichment. The actual Claude
invocation is never called — we test everything up to that boundary.

INVARIANT: all figmaclaw commands must be valid Python. A syntax error
breaks the entire enrichment pipeline (24+ hours of silent CI failures).
"""

from __future__ import annotations

import json
import py_compile
import textwrap
from pathlib import Path
from unittest.mock import Mock

from click.testing import CliRunner

from figmaclaw.commands import claude_run as claude_run_mod
from figmaclaw.commands.claude_run import (
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

    def test_directory_glob(self, tmp_path: Path) -> None:
        """Directory target → glob matches."""
        pages = tmp_path / "figma" / "pages"
        self._make_page(pages / "a.md")
        self._make_page(pages / "b.md")
        (tmp_path / "figma" / "other.txt").write_text("not a match")
        result = collect_files(tmp_path / "figma", "**/*.md", changed_only=False)
        assert len(result) == 2

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

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p: [])
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p: False)
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

    def test_section_mode_stuck_detection_breaks_after_retries(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        md = tmp_path / "page.md"
        self._write_placeholder_page(md)

        sections = [{"node_id": "10:1", "name": "Auth", "pending_frames": 2}]
        # Initial load + two post-batch refreshes; third loop detects "stuck" and exits.
        pending = [sections, sections, sections]

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p: (True, 120))
        monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p: pending.pop(0))
        monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p: False)
        monkeypatch.setattr(
            claude_run_mod,
            "decide_next_batch",
            lambda **_: self._budget_decision(True, "[budget] go"),
        )
        monkeypatch.setattr(
            claude_run_mod,
            "_run_claude",
            lambda **_: claude_run_mod.ClaudeResult(exit_code=0),
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
        assert "STUCK:" in result.output
        assert "Verdict (row 2)" in result.output

    def test_dispatch_crash_is_always_red(self, tmp_path: Path, monkeypatch) -> None:
        md = tmp_path / "page.md"
        self._write_placeholder_page(md)

        monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p: (True, 1))
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
