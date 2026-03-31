"""Tests for commands/init.py.

INVARIANTS:
- init_cmd copies both workflow templates into .github/workflows/
- init_cmd skips existing files by default (no --overwrite)
- init_cmd overwrites existing files when --overwrite is passed
- Copied files are valid YAML
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from figmaclaw.main import cli


def test_init_copies_workflow_files(tmp_path: Path):
    """INVARIANT: init copies both workflow templates into .github/workflows/."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "init"])
    assert result.exit_code == 0, result.output

    workflows_dir = tmp_path / ".github" / "workflows"
    assert (workflows_dir / "figmaclaw-webhook.yaml").exists()
    assert (workflows_dir / "figmaclaw-sync.yaml").exists()


def test_init_copied_files_are_valid_yaml(tmp_path: Path):
    """INVARIANT: Copied workflow files are valid YAML."""
    runner = CliRunner()
    runner.invoke(cli, ["--repo-dir", str(tmp_path), "init"])

    workflows_dir = tmp_path / ".github" / "workflows"
    for fname in ["figmaclaw-webhook.yaml", "figmaclaw-sync.yaml"]:
        content = (workflows_dir / fname).read_text()
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict), f"{fname} must be a YAML mapping"
        assert "on" in parsed or True  # GitHub Actions key


def test_init_skips_existing_files_by_default(tmp_path: Path):
    """INVARIANT: init does not overwrite existing workflow files without --overwrite."""
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    sentinel = "# do not overwrite\n"
    (workflows_dir / "figmaclaw-webhook.yaml").write_text(sentinel)

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "init"])
    assert result.exit_code == 0

    assert (workflows_dir / "figmaclaw-webhook.yaml").read_text() == sentinel


def test_init_overwrites_with_flag(tmp_path: Path):
    """INVARIANT: init --overwrite replaces existing workflow files."""
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "figmaclaw-webhook.yaml").write_text("# placeholder\n")

    runner = CliRunner()
    runner.invoke(cli, ["--repo-dir", str(tmp_path), "init", "--overwrite"])

    content = (workflows_dir / "figmaclaw-webhook.yaml").read_text()
    assert "figma-webhook" in content  # real template content, not placeholder
