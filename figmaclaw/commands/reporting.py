"""Shared report emission helpers for read-only commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from figmaclaw.figma_utils import write_json_if_changed

STDOUT_PATH = "-"


def resolve_repo_path(repo_dir: Path, path: Path) -> Path:
    """Resolve a command path relative to the target repo."""
    return path if path.is_absolute() else repo_dir / path


def resolve_output_path(repo_dir: Path, path: Path) -> Path:
    """Resolve a command output path relative to the target repo."""
    return resolve_repo_path(repo_dir, path)


def is_stdout_path(path: Path | None) -> bool:
    return path is not None and str(path) == STDOUT_PATH


def emit_json_value(data: dict[str, Any]) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=True))


def write_json_output(repo_dir: Path, path: Path, data: dict[str, Any]) -> bool:
    """Write JSON to a path, or stdout when the path is '-'."""
    if is_stdout_path(path):
        emit_json_value(data)
        return True
    write_json_if_changed(resolve_output_path(repo_dir, path), data)
    return False


def emit_json_report(
    ctx: click.Context,
    *,
    repo_dir: Path,
    report_data: dict[str, Any],
    out_path: Path | None,
    json_output: bool,
) -> bool:
    """Write optional report file and emit JSON when requested.

    Returns True when JSON was printed and the caller should skip human output.
    """
    if out_path is not None and write_json_output(repo_dir, out_path, report_data):
        return True
    if json_output or ctx.obj.get("json"):
        emit_json_value(report_data)
        return True
    return False
