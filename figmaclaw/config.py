"""Repository/user configuration for figmaclaw behavior flags."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FigmaclawConfig:
    """Small config surface for behavior that depends on Figma plan features."""

    license_type: str = "professional"

    def is_enterprise(self) -> bool:
        return self.license_type == "enterprise"


def load_config(repo_dir: Path) -> FigmaclawConfig:
    """Load figmaclaw config with conservative, non-Enterprise defaults.

    Supported config shapes:

    * ``pyproject.toml``: ``[tool.figmaclaw] license_type = "enterprise"``
    * ``figmaclaw.json`` or ``.figmaclaw.json`` with the same keys
    * env override: ``FIGMACLAW_LICENSE_TYPE=enterprise``
    """
    values: dict[str, Any] = {}
    values.update(_load_pyproject_config(repo_dir))
    values.update(_load_json_config(repo_dir))

    env_license_type = os.environ.get("FIGMACLAW_LICENSE_TYPE", "").strip()
    if env_license_type:
        values["license_type"] = env_license_type

    license_type = str(values.get("license_type") or "professional").strip().lower()
    return FigmaclawConfig(license_type=license_type)


def is_enterprise_license(repo_dir: Path) -> bool:
    return load_config(repo_dir).is_enterprise()


def _load_pyproject_config(repo_dir: Path) -> dict[str, Any]:
    path = repo_dir / "pyproject.toml"
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    config = data.get("tool", {}).get("figmaclaw", {})
    return config if isinstance(config, dict) else {}


def _load_json_config(repo_dir: Path) -> dict[str, Any]:
    for name in ("figmaclaw.json", ".figmaclaw.json"):
        path = repo_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            config = data.get("figmaclaw", data)
            if isinstance(config, dict):
                return config
    return {}
