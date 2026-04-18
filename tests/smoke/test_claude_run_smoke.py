"""Smoke tests for claude-run enrichment selection.

These are local integration smoke tests (no network) that exercise the real
CLI path used in CI for selecting files to enrich.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from figmaclaw.commands import claude_run as claude_run_mod
from figmaclaw.main import cli


def _write_page(
    path: Path, *, enriched: bool, placeholder: bool, schema_version: int | None = None
) -> None:
    desc = "(no description yet)" if placeholder else "already described"
    fm = [
        "---",
        "file_key: smoke123",
        'page_node_id: "0:1"',
    ]
    if enriched:
        fm.append('enriched_hash: "sha256:abc"')
    if schema_version is not None:
        fm.append(f"enriched_schema_version: {schema_version}")
    fm.extend(
        [
            "---",
            "",
            "| Screen | Node ID | Description |",
            "|--------|---------|-------------|",
            f"| A | `1:1` | {desc} |",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm))


@pytest.mark.smoke
def test_claude_run_needs_enrichment_backfills_placeholder_pages(tmp_path: Path) -> None:
    """Smoke: enriched_hash pages with placeholders are still queued for enrichment."""
    pages = tmp_path / "figma" / "web-app" / "pages"
    md_placeholder = pages / "legacy-placeholder.md"
    md_clean = pages / "already-enriched.md"
    _write_page(md_placeholder, enriched=True, placeholder=True, schema_version=1)
    _write_page(md_clean, enriched=True, placeholder=False, schema_version=1)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(tmp_path / "figma"),
            "--needs-enrichment",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    out = result.output
    assert str(md_placeholder) in out
    assert str(md_clean) not in out


@pytest.mark.smoke
def test_claude_run_needs_enrichment_excludes_census_files(tmp_path: Path) -> None:
    """Smoke: _census.md must never be queued as an enrichable page."""
    figma_file_dir = tmp_path / "figma" / "design-system-abc123"
    pages = figma_file_dir / "pages"
    md_pending = pages / "pending-page.md"
    _write_page(md_pending, enriched=False, placeholder=True)

    census_md = figma_file_dir / "_census.md"
    census_md.parent.mkdir(parents=True, exist_ok=True)
    census_md.write_text(
        "\n".join(
            [
                "---",
                "file_key: abc123",
                "---",
                "",
                "| Component set | Key | Page | Updated |",
                "|---|---|---|---|",
                "| `Button` | `k1` | Components | 2026-04-15 |",
            ]
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(tmp_path / "figma"),
            "--needs-enrichment",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    out = result.output
    assert str(md_pending) in out
    assert str(census_md) not in out


@pytest.mark.smoke
def test_claude_run_dry_run_also_excludes_census_files(tmp_path: Path) -> None:
    """Smoke: _census.md is excluded even without --needs-enrichment."""
    figma_file_dir = tmp_path / "figma" / "design-system-abc123"
    pages = figma_file_dir / "pages"
    md_page = pages / "pending-page.md"
    _write_page(md_page, enriched=False, placeholder=True)

    census_md = figma_file_dir / "_census.md"
    census_md.parent.mkdir(parents=True, exist_ok=True)
    census_md.write_text(
        "\n".join(
            [
                "---",
                "file_key: abc123",
                "---",
                "",
                "| Component set | Key | Page | Updated |",
                "|---|---|---|---|",
                "| `Button` | `k1` | Components | 2026-04-15 |",
            ]
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(tmp_path / "figma"),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    out = result.output
    assert str(md_page) in out
    assert str(census_md) not in out


@pytest.mark.smoke
def test_section_mode_smoke_stops_after_no_progress_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: same unresolved frame set should stop file in same run (no loop)."""
    source = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "linear_git_real"
        / "live_ui_unavailable_rows.md"
    )
    page = tmp_path / "figma" / "pages" / "live-ui.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    sections = [{"node_id": "10:1", "name": "Auth", "pending_frames": 2}]
    pending = [sections, sections]
    monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
    monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: pending.pop(0))
    monkeypatch.setattr(
        claude_run_mod, "pending_frame_node_ids", lambda _p, **_kw: {"11:1", "11:2"}
    )
    monkeypatch.setattr(
        claude_run_mod, "_is_schema_upgrade_only_candidate", lambda _p, **_kw: False
    )
    monkeypatch.setattr(claude_run_mod, "_classify_no_work_candidate", lambda _p, **_kw: "phantom")
    monkeypatch.setattr(
        claude_run_mod.subprocess,
        "run",
        lambda *args, **kwargs: Mock(stdout="", returncode=0),
    )
    run_mock = Mock(return_value=claude_run_mod.ClaudeResult(exit_code=0))
    monkeypatch.setattr(claude_run_mod, "_run_claude", run_mock)
    monkeypatch.setattr(claude_run_mod, "count_commits_since", lambda *_args, **_kwargs: 1)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(page),
            "--section-mode",
            "--prompt",
            "noop {file_path}",
        ],
    )

    assert result.exit_code == 0
    assert "NO-PROGRESS" in result.output
    assert run_mock.call_count == 1


@pytest.mark.smoke
def test_section_mode_smoke_phantom_selection_is_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: phantom selection should turn run red immediately."""
    page_a = tmp_path / "a.md"
    page_b = tmp_path / "b.md"
    _write_page(page_a, enriched=False, placeholder=True)
    _write_page(page_b, enriched=False, placeholder=True)

    monkeypatch.setattr(claude_run_mod, "collect_files", lambda *args, **kwargs: [page_a, page_b])
    monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
    monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: [])
    monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
    monkeypatch.setattr(
        claude_run_mod, "_is_schema_upgrade_only_candidate", lambda _p, **_kw: False
    )
    monkeypatch.setattr(claude_run_mod, "_classify_no_work_candidate", lambda _p, **_kw: "phantom")
    monkeypatch.setattr(
        claude_run_mod.subprocess,
        "run",
        lambda *args, **kwargs: Mock(stdout="", returncode=0),
    )

    monkeypatch.setattr(claude_run_mod, "count_commits_since", lambda *_args, **_kwargs: 0)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(page_a),
            "--section-mode",
            "--prompt",
            "noop {file_path}",
        ],
    )

    assert result.exit_code == 2
    assert "PHANTOM SELECTION" in result.output
    assert "[2/2]" not in result.output


@pytest.mark.smoke
def test_section_mode_smoke_llm_marker_only_is_non_phantom_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: marker-only section candidates should skip without phantom red."""
    page = tmp_path / "marker-only.md"
    page.write_text(
        """---
file_key: smoke123
page_node_id: "0:1"
enriched_hash: deadbeef
enriched_schema_version: 1
---

<!-- LLM: section rewrite needed -->

| Screen | Node ID | Description |
|--------|---------|-------------|
| A | `1:1` | already described |
"""
    )

    monkeypatch.setattr(claude_run_mod, "enrichment_info", lambda _p, **_kw: (True, 120))
    monkeypatch.setattr(claude_run_mod, "pending_sections", lambda _p, **_kw: [])
    monkeypatch.setattr(claude_run_mod, "needs_finalization", lambda _p, **_kw: False)
    monkeypatch.setattr(
        claude_run_mod, "_is_schema_upgrade_only_candidate", lambda _p, **_kw: False
    )
    monkeypatch.setattr(claude_run_mod, "_is_llm_marker_only_candidate", lambda _p, **_kw: True)
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
            str(page),
            "--section-mode",
            "--prompt",
            "noop {file_path}",
        ],
    )

    assert result.exit_code == 0
    assert "skip (LLM-marker-only candidate)" in result.output
    assert "PHANTOM SELECTION" not in result.output
