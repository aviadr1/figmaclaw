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

    def is_enterprise(self) -> bool:
        return self.license_type == "enterprise"


def load_config(repo_dir: Path) -> FigmaclawConfig:
    """Load figmaclaw config with conservative, non-Enterprise defaults.

    Supported config shapes:

    * ``pyproject.toml``: ``[tool.figmaclaw] license_type = "enterprise"``
    * ``pyproject.toml``:
      ``[tool.figmaclaw.design_system] library_hashes = ["..."]``
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
    design_system = values.get("design_system")
    if not isinstance(design_system, dict):
        return ()
    raw_hashes = design_system.get("library_hashes")
    if isinstance(raw_hashes, str):
        raw_values: list[Any] = [raw_hashes]
    elif isinstance(raw_hashes, list):
        raw_values = raw_hashes
    else:
        return ()
    hashes = []
    for value in raw_values:
        text = str(value).strip()
        if text:
            hashes.append(text)
    return tuple(dict.fromkeys(hashes))
