"""Tests for commands/init.py.

INVARIANTS:
- init_cmd copies all managed workflow templates into .github/workflows/
- init_cmd skips existing files by default (no --overwrite)
- init_cmd overwrites existing files when --overwrite is passed
- Copied files are valid YAML
"""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from figmaclaw.main import cli

_MANAGED_WORKFLOWS = (
    "figmaclaw-webhook.yaml",
    "figmaclaw-sync.yaml",
    "figmaclaw-manage-webhooks.yaml",
)


def test_init_copies_workflow_files(tmp_path: Path):
    """INVARIANT: init copies all managed workflow templates into .github/workflows/."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "init"])
    assert result.exit_code == 0, result.output

    workflows_dir = tmp_path / ".github" / "workflows"
    for name in _MANAGED_WORKFLOWS:
        assert (workflows_dir / name).exists()


def test_init_copied_files_are_valid_yaml(tmp_path: Path):
    """INVARIANT: Copied workflow files are valid YAML."""
    runner = CliRunner()
    runner.invoke(cli, ["--repo-dir", str(tmp_path), "init"])

    workflows_dir = tmp_path / ".github" / "workflows"
    for fname in _MANAGED_WORKFLOWS:
        content = (workflows_dir / fname).read_text()
        parsed = yaml.safe_load(content)
        assert isinstance(parsed, dict), f"{fname} must be a YAML mapping"
        assert ("on" in parsed) or (True in parsed)  # YAML 1.1 may parse "on" as True


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


def test_init_with_webhook_proxy_copies_worker(tmp_path: Path):
    """INVARIANT: --with-webhook-proxy copies the CF Worker template."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "init", "--with-webhook-proxy"])
    assert result.exit_code == 0, result.output

    proxy_dir = tmp_path / "workers" / "figma-webhook-proxy"
    assert (proxy_dir / "src" / "index.js").exists()
    assert (proxy_dir / "wrangler.toml").exists()

    wrangler = (proxy_dir / "wrangler.toml").read_text()
    assert 'GITHUB_REPO = "OWNER/REPO"' in wrangler
    assert "YOUR_KV_NAMESPACE_ID" in wrangler


def test_init_webhook_proxy_skips_existing(tmp_path: Path):
    """INVARIANT: --with-webhook-proxy does not overwrite existing proxy dir without --overwrite."""
    proxy_dir = tmp_path / "workers" / "figma-webhook-proxy"
    proxy_dir.mkdir(parents=True)
    sentinel = proxy_dir / "sentinel.txt"
    sentinel.write_text("keep me")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(tmp_path), "init", "--with-webhook-proxy"])
    assert result.exit_code == 0

    assert sentinel.read_text() == "keep me"


def test_init_webhook_proxy_overwrites_with_flag(tmp_path: Path):
    """INVARIANT: --with-webhook-proxy --overwrite replaces the proxy dir."""
    proxy_dir = tmp_path / "workers" / "figma-webhook-proxy"
    proxy_dir.mkdir(parents=True)
    (proxy_dir / "old-file.txt").write_text("stale")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["--repo-dir", str(tmp_path), "init", "--with-webhook-proxy", "--overwrite"]
    )
    assert result.exit_code == 0

    assert (proxy_dir / "src" / "index.js").exists()
    assert not (proxy_dir / "old-file.txt").exists()
