from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


def _load_check_module():
    path = Path(__file__).parents[1] / "scripts" / "check_version_bump.py"
    spec = importlib.util.spec_from_file_location("check_version_bump", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _write_version_artifacts(repo: Path, version: str) -> None:
    (repo / ".claude-plugin").mkdir(exist_ok=True)
    (repo / "figmaclaw").mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "figmaclaw"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (repo / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "figmaclaw", "version": version}) + "\n",
        encoding="utf-8",
    )
    (repo / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"plugins": [{"name": "figmaclaw", "version": version}]}) + "\n",
        encoding="utf-8",
    )
    (repo / "figmaclaw" / "_build_info.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text(
        f'name = "figmaclaw"\nversion = "{version}"\n',
        encoding="utf-8",
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_check_version_bump_accepts_complete_source_controlled_bump(tmp_path: Path) -> None:
    module = _load_check_module()
    _init_repo(tmp_path)
    _write_version_artifacts(tmp_path, "1.2.3")
    base = _commit(tmp_path, "base")

    _write_version_artifacts(tmp_path, "1.2.4")
    _commit(tmp_path, "bump version")

    assert module.check_version_bump(tmp_path, base) == []


def test_check_version_bump_rejects_pr_without_version_change(tmp_path: Path) -> None:
    module = _load_check_module()
    _init_repo(tmp_path)
    _write_version_artifacts(tmp_path, "1.2.3")
    base = _commit(tmp_path, "base")

    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    _commit(tmp_path, "docs only")

    errors = module.check_version_bump(tmp_path, base)

    assert any("version must increase" in error for error in errors)
    assert any("required artifacts did not change" in error for error in errors)


def test_check_version_bump_rejects_inconsistent_artifacts(tmp_path: Path) -> None:
    module = _load_check_module()
    _init_repo(tmp_path)
    _write_version_artifacts(tmp_path, "1.2.3")
    base = _commit(tmp_path, "base")

    _write_version_artifacts(tmp_path, "1.2.4")
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "figmaclaw", "version": "1.2.3"}) + "\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "partial bump")

    errors = module.check_version_bump(tmp_path, base)

    assert ".claude-plugin/plugin.json version does not match pyproject.toml" in errors


def test_check_version_bump_reports_malformed_artifacts_without_traceback(
    tmp_path: Path,
) -> None:
    module = _load_check_module()
    _init_repo(tmp_path)
    _write_version_artifacts(tmp_path, "1.2.3")
    base = _commit(tmp_path, "base")

    _write_version_artifacts(tmp_path, "1.2.4")
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
    _commit(tmp_path, "malformed bump")

    errors = module.check_version_bump(tmp_path, base)

    assert any(".claude-plugin/plugin.json is missing or malformed" in error for error in errors)


def test_check_version_bump_main_prints_structured_error_for_bad_versions(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_check_module()
    _init_repo(tmp_path)
    _write_version_artifacts(tmp_path, "1.2.3")
    base = _commit(tmp_path, "base")

    _write_version_artifacts(tmp_path, "not-a-version")
    _commit(tmp_path, "bad version")

    try:
        module.main(["--repo-root", str(tmp_path), "--base-ref", base])
    except SystemExit as exc:
        assert exc.code == 1

    assert "::error::" in capsys.readouterr().out
