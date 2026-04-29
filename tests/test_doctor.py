"""Tests for commands/doctor.py.

INVARIANTS:
- doctor exits 0 when all required checks pass
- doctor exits 1 when FIGMA_API_KEY is missing
- doctor reports manifest status correctly
- doctor reports workflow file status correctly
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
from figmaclaw.main import cli


def _init_git(tmp_path: Path) -> None:
    """Create a minimal git repo."""
    (tmp_path / ".git").mkdir()


def test_doctor_fails_without_api_key(tmp_path: Path) -> None:
    """INVARIANT: doctor exits 1 when FIGMA_API_KEY is missing."""
    _init_git(tmp_path)
    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "doctor",
            ],
        )
    assert result.exit_code == 1
    assert "FIGMA_API_KEY" in result.output


def test_doctor_reports_missing_manifest(tmp_path: Path) -> None:
    """INVARIANT: doctor warns about missing manifest."""
    _init_git(tmp_path)
    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "doctor",
            ],
        )
    assert "manifest" in result.output.lower()
    assert "figmaclaw track" in result.output


def test_doctor_reports_missing_workflows(tmp_path: Path) -> None:
    """INVARIANT: doctor warns about missing workflow files."""
    _init_git(tmp_path)
    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "doctor",
            ],
        )
    assert "figmaclaw init" in result.output


def test_doctor_detects_workflow_files(tmp_path: Path) -> None:
    """INVARIANT: doctor reports found workflow files."""
    _init_git(tmp_path)
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "figmaclaw-sync.yaml").write_text("name: sync")
    (wf_dir / "figmaclaw-webhook.yaml").write_text("name: webhook")

    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "doctor",
            ],
        )
    assert "figmaclaw-sync.yaml" in result.output
    assert "figmaclaw-webhook.yaml" in result.output


def test_doctor_reports_partial_pull_pages(tmp_path: Path) -> None:
    """INVARIANT PP-1: doctor surfaces pages stuck in the partial-pull shape
    (md_path=None AND component_md_paths=[]). This was the silent
    failure mode that affected 215 pages of linear-git for months
    before PR 129. Detecting it in doctor lets consumer repos see the
    bug count before/after a figmaclaw upgrade."""
    _init_git(tmp_path)
    sync_dir = tmp_path / ".figma-sync"
    sync_dir.mkdir()
    manifest = {
        "version": 1,
        "tracked_files": ["abc"],
        "files": {
            "abc": {
                "file_name": "Test File",
                "version": "v1",
                "last_modified": "2026-04-29T00:00:00Z",
                "pull_schema_version": 9,
                "pages": {
                    "1:0": {
                        "page_name": "Stuck page",
                        "page_slug": "stuck-1-0",
                        "md_path": None,
                        "page_hash": "deadbeefdeadbeef",
                        "last_refreshed_at": "2026-04-29T00:00:00Z",
                        "component_md_paths": [],
                        "frame_hashes": {},
                    },
                },
            }
        },
        "skipped_files": {},
    }
    (sync_dir / "manifest.json").write_text(json.dumps(manifest))

    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "doctor"])
    assert "partial-pull" in result.output, result.output
    assert "Stuck page" in result.output, result.output


def test_doctor_does_not_report_partial_pull_for_component_only_pages(tmp_path: Path) -> None:
    """INVARIANT PP-1: a page with md_path=None but component_md_paths populated is a
    valid component-only page (the H2 fix makes this the default state
    for top-level COMPONENT_SET pages). Must NOT be flagged as a
    partial-pull — false positives would erode trust in the check."""
    _init_git(tmp_path)
    sync_dir = tmp_path / ".figma-sync"
    sync_dir.mkdir()
    manifest = {
        "version": 1,
        "tracked_files": ["abc"],
        "files": {
            "abc": {
                "file_name": "Test File",
                "version": "v1",
                "last_modified": "2026-04-29T00:00:00Z",
                "pull_schema_version": 9,
                "pages": {
                    "1:0": {
                        "page_name": "Component-only page",
                        "page_slug": "component-only-1-0",
                        "md_path": None,
                        "page_hash": "deadbeefdeadbeef",
                        "last_refreshed_at": "2026-04-29T00:00:00Z",
                        "component_md_paths": ["figma/x/components/c-1-1.md"],
                        "frame_hashes": {},
                    },
                },
            }
        },
        "skipped_files": {},
    }
    (sync_dir / "manifest.json").write_text(json.dumps(manifest))

    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "doctor"])
    # The success path runs the partial-pull check and reports zero.
    # A false-positive would re-introduce the warning.
    assert "1 page(s) with md_path=null" not in result.output, result.output


def test_doctor_counts_all_partial_pull_pages_not_only_first_five(tmp_path: Path) -> None:
    """INVARIANT PP-1: doctor reports the true partial-pull count.

    PR 129 found hundreds of linear-git entries in the empty-list-hash shape.
    Capping collection at five hides the blast radius and makes before/after
    proof impossible.
    """

    _init_git(tmp_path)
    sync_dir = tmp_path / ".figma-sync"
    sync_dir.mkdir()
    pages = {
        f"{idx}:0": {
            "page_name": f"Stuck page {idx}",
            "page_slug": f"stuck-{idx}-0",
            "md_path": None,
            "page_hash": "4f53cda18c2baa0c",
            "last_refreshed_at": "2026-04-29T00:00:00Z",
            "component_md_paths": [],
            "frame_hashes": {},
        }
        for idx in range(6)
    }
    manifest = {
        "version": 1,
        "tracked_files": ["abc"],
        "files": {
            "abc": {
                "file_name": "Test File",
                "version": "v1",
                "last_modified": "2026-04-29T00:00:00Z",
                "pull_schema_version": 9,
                "pages": pages,
            }
        },
        "skipped_files": {},
    }
    (sync_dir / "manifest.json").write_text(json.dumps(manifest))

    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "doctor"])

    assert "6 page(s) with md_path=null" in result.output, result.output
    assert "6 with empty-list hash" in result.output, result.output
    assert "+3 more" in result.output, result.output


def test_doctor_ignores_partial_shape_when_page_matches_skip_pages(tmp_path: Path) -> None:
    """INVARIANT: deliberate skip pages are not actionable partial pulls."""

    _init_git(tmp_path)
    sync_dir = tmp_path / ".figma-sync"
    sync_dir.mkdir()
    manifest = {
        "version": 1,
        "skip_pages": ["---", "archive*"],
        "tracked_files": ["abc"],
        "files": {
            "abc": {
                "file_name": "Test File",
                "version": "v1",
                "last_modified": "2026-04-29T00:00:00Z",
                "pull_schema_version": 9,
                "pages": {
                    "1:0": {
                        "page_name": "---",
                        "page_slug": "separator-1-0",
                        "md_path": None,
                        "page_hash": "4f53cda18c2baa0c",
                        "last_refreshed_at": "2026-04-29T00:00:00Z",
                        "component_md_paths": [],
                        "frame_hashes": {},
                    },
                    "2:0": {
                        "page_name": "Archive notes",
                        "page_slug": "archive-notes-2-0",
                        "md_path": None,
                        "page_hash": "4f53cda18c2baa0c",
                        "last_refreshed_at": "2026-04-29T00:00:00Z",
                        "component_md_paths": [],
                        "frame_hashes": {},
                    },
                },
            }
        },
        "skipped_files": {},
    }
    (sync_dir / "manifest.json").write_text(json.dumps(manifest))

    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "doctor"])

    assert "no partial-pull pages" in result.output, result.output
    assert "2 skipped empty page(s) matched skip_pages" in result.output, result.output
    assert "page(s) with md_path=null" not in result.output, result.output


def test_doctor_reports_files_below_current_pull_schema(tmp_path: Path) -> None:
    """INVARIANT: doctor surfaces schema-backlog files before users see stale pages."""

    _init_git(tmp_path)
    sync_dir = tmp_path / ".figma-sync"
    sync_dir.mkdir()
    manifest = {
        "version": 1,
        "tracked_files": ["abc", "def"],
        "files": {
            "abc": {
                "file_name": "Old schema file",
                "version": "v1",
                "last_modified": "2026-04-29T00:00:00Z",
                "pull_schema_version": CURRENT_PULL_SCHEMA_VERSION - 1,
                "pages": {},
            },
            "def": {
                "file_name": "Current schema file",
                "version": "v1",
                "last_modified": "2026-04-29T00:00:00Z",
                "pull_schema_version": CURRENT_PULL_SCHEMA_VERSION,
                "pages": {},
            },
        },
        "skipped_files": {},
    }
    (sync_dir / "manifest.json").write_text(json.dumps(manifest))

    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "doctor"])

    assert "pull schema current" in result.output, result.output
    assert "1 file(s) below" in result.output, result.output
    assert "Old schema file" in result.output, result.output
    assert "Current schema file" not in result.output, result.output


def test_doctor_detects_figma_pages(tmp_path: Path) -> None:
    """INVARIANT: doctor counts .md files in figma/ directory."""
    _init_git(tmp_path)
    figma_dir = tmp_path / "figma" / "test-file" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "page-1.md").write_text("---\nfile_key: x\n---\n")
    (figma_dir / "page-2.md").write_text("---\nfile_key: x\n---\n")

    runner = CliRunner()
    with patch.dict("os.environ", {"FIGMA_API_KEY": ""}, clear=False):
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "doctor",
            ],
        )
    assert "2 .md file(s)" in result.output
