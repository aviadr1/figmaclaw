"""Managed workflow template helpers.

This module is the single source of truth for workflow stub files that
`figmaclaw` installs into host repositories.
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MANAGED_MARKER = "# Installed by: figmaclaw init"


@dataclass
class WorkflowTemplateStatus:
    """Comparison result for one managed workflow template in a target repo."""

    name: str
    exists: bool
    managed: bool
    in_sync: bool

    @property
    def healthy(self) -> bool:
        return self.exists and self.managed and self.in_sync


def _templates_pkg() -> Any:
    return importlib.resources.files("figmaclaw") / "workflows"


def workflow_template_files() -> tuple[str, ...]:
    """Return managed workflow template filenames in deterministic order."""
    names: list[str] = []
    for entry in _templates_pkg().iterdir():
        if entry.is_file() and entry.name.endswith(".yaml") and entry.name.startswith("figmaclaw-"):
            names.append(entry.name)
    return tuple(sorted(names))


def bundled_template_text(name: str) -> str:
    """Load a bundled workflow template by filename."""
    return (_templates_pkg() / name).read_text()


def copy_workflow_templates(repo_dir: Path, workflows_dir: str = ".github/workflows") -> list[str]:
    """Copy all managed workflow templates into the target workflows directory."""
    wf_dir = repo_dir / workflows_dir
    wf_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for name in workflow_template_files():
        dst = wf_dir / name
        dst.write_text(bundled_template_text(name))
        written.append(name)
    return written


def collect_workflow_status(repo_dir: Path) -> list[WorkflowTemplateStatus]:
    """Return existence/management/sync status for all managed templates."""
    wf_dir = repo_dir / ".github" / "workflows"
    statuses: list[WorkflowTemplateStatus] = []

    for name in workflow_template_files():
        dst = wf_dir / name
        expected = bundled_template_text(name)

        if not dst.exists():
            statuses.append(
                WorkflowTemplateStatus(
                    name=name,
                    exists=False,
                    managed=False,
                    in_sync=False,
                )
            )
            continue

        actual = dst.read_text()
        statuses.append(
            WorkflowTemplateStatus(
                name=name,
                exists=True,
                managed=_MANAGED_MARKER in actual,
                in_sync=(actual == expected),
            )
        )

    return statuses
