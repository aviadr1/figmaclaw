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
    design_system_library_hashes: tuple[str, ...] = ()
    design_system_file_keys: tuple[str, ...] = ()
    design_system_published_keys: tuple[str, ...] = ()

    def is_enterprise(self) -> bool:
        return self.license_type == "enterprise"


def load_config(repo_dir: Path) -> FigmaclawConfig:
    """Load figmaclaw config with conservative, non-Enterprise defaults.

    Supported config shapes:

    * ``pyproject.toml``: ``[tool.figmaclaw] license_type = "enterprise"``
    * ``pyproject.toml``:
      ``[tool.figmaclaw.design_system] library_hashes = ["..."]``
    * ``pyproject.toml``:
      ``[tool.figmaclaw.design_system] file_keys = ["..."]``
    * ``pyproject.toml``:
      ``[tool.figmaclaw.design_system] published_keys = ["..."]``
    * ``figmaclaw.json`` or ``.figmaclaw.json`` with the same keys
    * env override: ``FIGMACLAW_LICENSE_TYPE=enterprise``
    * env override:
      ``FIGMACLAW_CURRENT_DS_LIBRARY_HASHES=hash1,hash2``
    """
    values: dict[str, Any] = {}
    values.update(_load_pyproject_config(repo_dir))
    values.update(_load_json_config(repo_dir))

    env_license_type = os.environ.get("FIGMACLAW_LICENSE_TYPE", "").strip()
    if env_license_type:
        values["license_type"] = env_license_type

    env_hashes = os.environ.get("FIGMACLAW_CURRENT_DS_LIBRARY_HASHES", "").strip()
    if env_hashes:
        design_system = values.get("design_system")
        if not isinstance(design_system, dict):
            design_system = {}
            values["design_system"] = design_system
        design_system["library_hashes"] = [part.strip() for part in env_hashes.split(",")]

    license_type = str(values.get("license_type") or "professional").strip().lower()
    return FigmaclawConfig(
        license_type=license_type,
        design_system_library_hashes=_design_system_hashes(values),
        design_system_file_keys=_design_system_values(values, "file_keys"),
        design_system_published_keys=_design_system_values(values, "published_keys"),
    )


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


def _design_system_hashes(values: dict[str, Any]) -> tuple[str, ...]:
    return _design_system_values(values, "library_hashes")


def _design_system_values(values: dict[str, Any], key: str) -> tuple[str, ...]:
    design_system = values.get("design_system")
    if not isinstance(design_system, dict):
        return ()
    raw_values = design_system.get(key)
    if isinstance(raw_values, str):
        values_list: list[Any] = [raw_values]
    elif isinstance(raw_values, list):
        values_list = raw_values
    else:
        return ()
    normalized = []
    for value in values_list:
        text = str(value).strip()
        if text:
            normalized.append(text)
    return tuple(dict.fromkeys(normalized))
