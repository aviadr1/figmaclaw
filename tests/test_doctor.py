"""Tests for commands/doctor.py.

INVARIANTS:
- doctor exits 0 when all required checks pass
- doctor exits 1 when FIGMA_API_KEY is missing
- doctor reports manifest status correctly
- doctor reports workflow file status correctly
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

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
