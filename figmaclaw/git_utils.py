"""Shared git helpers used by pull, sync, set-flows, write-body, mark-enriched, and apply-webhook commands."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_commit(repo_dir: Path, paths: list[str], message: str) -> bool:
    """Stage the given paths and commit if anything changed. Returns True if committed."""
    subprocess.run(["git", "-C", str(repo_dir), "add", *paths], check=False)
    diff = subprocess.run(["git", "-C", str(repo_dir), "diff", "--cached", "--quiet"], check=False)
    if diff.returncode == 0:
        return False
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", message], check=False)
    return True


def git_push(repo_dir: Path) -> None:
    """Push to origin; on conflict, pull --no-rebase and retry once."""
    result = subprocess.run(["git", "-C", str(repo_dir), "push"], check=False)
    if result.returncode != 0:
        subprocess.run(["git", "-C", str(repo_dir), "pull", "--no-rebase"], check=False)
        subprocess.run(["git", "-C", str(repo_dir), "push"], check=False)
