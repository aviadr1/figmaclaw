from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from pathlib import Path


def _load_bump_module():
    path = Path(__file__).parents[1] / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bump_version_updates_all_version_artifacts(tmp_path: Path) -> None:
    module = _load_bump_module()

    (tmp_path / "figmaclaw").mkdir()
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "figmaclaw"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "figmaclaw", "version": "1.2.3"}),
        encoding="utf-8",
    )
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "figmaclaw", "version": "1.2.3"},
                    {"name": "other", "version": "9.9.9"},
                ]
            }
        ),
        encoding="utf-8",
    )

    new_version = module.bump_version(
        tmp_path,
        "abcdef1234567890",
        "feat: merge useful work (#129)\n\nbody",
    )

    assert new_version == "1.2.4"
    assert 'version = "1.2.4"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

    plugin = json.loads((tmp_path / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] == "1.2.4"

    marketplace = json.loads(
        (tmp_path / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert marketplace["plugins"] == [
        {"name": "figmaclaw", "version": "1.2.4"},
        {"name": "other", "version": "9.9.9"},
    ]

    build_info = (tmp_path / "figmaclaw" / "_build_info.py").read_text(encoding="utf-8")
    assert '__version__ = "1.2.4"' in build_info
    assert '__commit__ = "abcdef1234567890"' in build_info
    assert '__pr__ = "129"' in build_info


def test_committed_version_artifacts_are_consistent() -> None:
    """INVARIANT: release version changes are committed with the code they describe."""

    repo_root = Path(__file__).parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]

    plugin = json.loads((repo_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (repo_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    build_info = (repo_root / "figmaclaw" / "_build_info.py").read_text(encoding="utf-8")
    lock = (repo_root / "uv.lock").read_text(encoding="utf-8")

    assert plugin["version"] == version
    figmaclaw_entry = next(p for p in marketplace["plugins"] if p["name"] == "figmaclaw")
    assert figmaclaw_entry["version"] == version
    assert f'__version__ = "{version}"' in build_info
    assert re.search(r'name = "figmaclaw"\nversion = "' + re.escape(version) + r'"', lock)
