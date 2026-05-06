from __future__ import annotations

from pathlib import Path

from figmaclaw.config import load_config


def test_config_defaults_to_non_enterprise(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.license_type == "professional"
    assert config.is_enterprise() is False


def test_config_reads_license_type_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.figmaclaw]\nlicense_type = "enterprise"\n',
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.license_type == "enterprise"
    assert config.is_enterprise() is True


def test_config_env_license_type_overrides_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.figmaclaw]\nlicense_type = "enterprise"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("FIGMACLAW_LICENSE_TYPE", "professional")

    config = load_config(tmp_path)

    assert config.license_type == "professional"
    assert config.is_enterprise() is False
