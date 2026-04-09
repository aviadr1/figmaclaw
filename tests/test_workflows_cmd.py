"""Tests for `figmaclaw workflows` commands."""

from __future__ import annotations

import importlib.resources
import json

from click.testing import CliRunner

from figmaclaw.main import cli


def _workflow_dir(repo_dir):
    return repo_dir / ".github" / "workflows"


def test_workflows_upgrade_writes_all_managed_files(tmp_path):
    """INVARIANT: `workflows upgrade` installs all managed workflow stubs."""
    runner = CliRunner()
    result = runner.invoke(cli, ["workflows", "upgrade", "--repo-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    wf_dir = _workflow_dir(tmp_path)
    assert (wf_dir / "figmaclaw-sync.yaml").exists()
    assert (wf_dir / "figmaclaw-webhook.yaml").exists()
    assert (wf_dir / "figmaclaw-manage-webhooks.yaml").exists()


def test_workflows_upgrade_overwrites_drifted_managed_file(tmp_path):
    """INVARIANT: `workflows upgrade` repairs content drift for managed files."""
    wf_dir = _workflow_dir(tmp_path)
    wf_dir.mkdir(parents=True, exist_ok=True)
    drifted = wf_dir / "figmaclaw-sync.yaml"
    drifted.write_text("# drifted by hand\nname: drifted\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["workflows", "upgrade", "--repo-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output

    expected = (
        importlib.resources.files("figmaclaw") / "workflows" / "figmaclaw-sync.yaml"
    ).read_text()
    assert drifted.read_text() == expected


def test_workflows_doctor_reports_healthy_after_upgrade(tmp_path):
    """INVARIANT: `workflows doctor` reports healthy when templates are current."""
    runner = CliRunner()
    upgrade = runner.invoke(cli, ["workflows", "upgrade", "--repo-dir", str(tmp_path)])
    assert upgrade.exit_code == 0, upgrade.output

    result = runner.invoke(cli, ["workflows", "doctor", "--repo-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "figmaclaw-sync.yaml: ok" in result.output
    assert "figmaclaw-webhook.yaml: ok" in result.output
    assert "figmaclaw-manage-webhooks.yaml: ok" in result.output
    assert "present and up to date" in result.output


def test_workflows_doctor_strict_fails_on_drift(tmp_path):
    """INVARIANT: `workflows doctor --strict` fails when managed files are drifted."""
    runner = CliRunner()
    upgrade = runner.invoke(cli, ["workflows", "upgrade", "--repo-dir", str(tmp_path)])
    assert upgrade.exit_code == 0, upgrade.output

    drifted = _workflow_dir(tmp_path) / "figmaclaw-webhook.yaml"
    drifted.write_text("# Installed by: figmaclaw init\nname: drifted\n")

    result = runner.invoke(cli, ["workflows", "doctor", "--repo-dir", str(tmp_path), "--strict"])
    assert result.exit_code != 0
    assert "figmaclaw-webhook.yaml: drifted" in result.output
    assert "workflow doctor failed" in result.output.lower()


def test_workflows_doctor_json_output(tmp_path):
    """INVARIANT: `--json workflows doctor` emits structured per-file status."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--json", "workflows", "doctor", "--repo-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["healthy"] is False
    assert "figmaclaw-sync.yaml" in payload["expected"]
    assert "figmaclaw-webhook.yaml" in payload["expected"]
    assert "figmaclaw-manage-webhooks.yaml" in payload["expected"]
    assert any(file["name"] == "figmaclaw-sync.yaml" for file in payload["files"])
