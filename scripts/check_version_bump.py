#!/usr/bin/env python3
"""Fail PR CI unless figmaclaw's source-controlled version was bumped."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

VERSION_ARTIFACTS = (
    "pyproject.toml",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "figmaclaw/_build_info.py",
    "uv.lock",
)


def _run_git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _version_tuple(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3:
        raise RuntimeError(f"Expected MAJOR.MINOR.PATCH version, got {version!r}")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise RuntimeError(f"Expected numeric MAJOR.MINOR.PATCH version, got {version!r}") from exc


def _current_pyproject_version(repo_root: Path) -> str:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject.get("project", {}).get("version")
    if not isinstance(version, str):
        raise RuntimeError("Could not find project.version in pyproject.toml")
    return version


def _base_pyproject_version(repo_root: Path, base_ref: str) -> str:
    pyproject = tomllib.loads(_run_git(repo_root, "show", f"{base_ref}:pyproject.toml"))
    version = pyproject.get("project", {}).get("version")
    if not isinstance(version, str):
        raise RuntimeError(f"Could not find project.version in {base_ref}:pyproject.toml")
    return version


def _changed_files(repo_root: Path, base_ref: str) -> set[str]:
    out = _run_git(repo_root, "diff", "--name-only", f"{base_ref}...HEAD")
    return {line.strip() for line in out.splitlines() if line.strip()}


def _assert_current_artifacts_consistent(repo_root: Path, version: str) -> list[str]:
    errors: list[str] = []

    plugin_path = repo_root / ".claude-plugin" / "plugin.json"
    try:
        plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors.append(f".claude-plugin/plugin.json is missing or malformed: {exc}")
        plugin = {}
    if plugin.get("version") != version:
        errors.append(".claude-plugin/plugin.json version does not match pyproject.toml")

    marketplace_path = repo_root / ".claude-plugin" / "marketplace.json"
    try:
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors.append(f".claude-plugin/marketplace.json is missing or malformed: {exc}")
        marketplace = {}
    figmaclaw_entry = next(
        (entry for entry in marketplace.get("plugins", []) if entry.get("name") == "figmaclaw"),
        None,
    )
    if not figmaclaw_entry or figmaclaw_entry.get("version") != version:
        errors.append(".claude-plugin/marketplace.json figmaclaw version does not match")

    try:
        build_info = (repo_root / "figmaclaw" / "_build_info.py").read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError) as exc:
        errors.append(f"figmaclaw/_build_info.py is missing or malformed: {exc}")
    else:
        if f'__version__ = "{version}"' not in build_info:
            errors.append("figmaclaw/_build_info.py __version__ does not match")

    try:
        lock = (repo_root / "uv.lock").read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError) as exc:
        errors.append(f"uv.lock is missing or malformed: {exc}")
    else:
        if not re.search(r'name = "figmaclaw"\nversion = "' + re.escape(version) + r'"', lock):
            errors.append("uv.lock figmaclaw version does not match")

    return errors


def check_version_bump(repo_root: Path, base_ref: str) -> list[str]:
    base_version = _base_pyproject_version(repo_root, base_ref)
    current_version = _current_pyproject_version(repo_root)
    changed = _changed_files(repo_root, base_ref)
    errors: list[str] = []

    if _version_tuple(current_version) <= _version_tuple(base_version):
        errors.append(
            f"figmaclaw version must increase before merge: base={base_version}, "
            f"current={current_version}. Run scripts/bump_version.py and uv lock."
        )

    required_artifacts: set[str] = set(VERSION_ARTIFACTS)
    missing = sorted(required_artifacts - changed)
    if missing:
        errors.append(
            "version bump is incomplete; these required artifacts did not change: "
            + ", ".join(missing)
        )

    errors.extend(_assert_current_artifacts_consistent(repo_root, current_version))
    return errors


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--base-ref", required=True, help="Base commit/ref for the PR diff.")
    args = parser.parse_args(argv)

    try:
        errors = check_version_bump(args.repo_root, args.base_ref)
    except Exception as exc:
        errors = [str(exc)]
    if errors:
        for error in errors:
            print(f"::error::{error}")
        sys.exit(1)
    print("Version bump is present and consistent.")


if __name__ == "__main__":
    main()
