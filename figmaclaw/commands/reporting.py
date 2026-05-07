"""Shared report emission helpers for read-only commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from figmaclaw.figma_utils import write_json_if_changed


def resolve_repo_path(repo_dir: Path, path: Path) -> Path:
    """Resolve a command path relative to the target repo."""
    return path if path.is_absolute() else repo_dir / path


def resolve_output_path(repo_dir: Path, path: Path) -> Path:
    """Resolve a command output path relative to the target repo."""
    return resolve_repo_path(repo_dir, path)


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
    if out_path is not None:
        write_json_if_changed(resolve_output_path(repo_dir, out_path), report_data)
    if json_output or ctx.obj.get("json"):
        click.echo(json.dumps(report_data, indent=2, ensure_ascii=True))
        return True
    return False
