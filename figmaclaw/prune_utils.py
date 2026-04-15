"""Shared pruning helpers for generated figmaclaw artifacts."""

from __future__ import annotations

import re
from pathlib import Path

from figmaclaw.figma_paths import token_sidecar_path, token_sidecar_rel_to_md_rel
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry

_NODE_SUFFIX_RE = re.compile(r".*-\d+-\d+\.md$")


def entry_paths(entry: PageEntry) -> set[str]:
    """Return all generated markdown paths referenced by one manifest page entry."""
    paths: set[str] = set(entry.component_md_paths)
    if entry.md_path:
        paths.add(entry.md_path)
    return paths


def is_generated_md_relpath(rel_path: str) -> bool:
    """True when rel_path looks like a generated page/component markdown path."""
    path = Path(rel_path)
    parts = path.parts
    if len(parts) < 3 or parts[0] != "figma":
        return False
    if len(parts) == 3:
        return path.name == "_census.md"
    if len(parts) < 4:
        return False
    if parts[-2] not in {"pages", "components"}:
        return False
    if path.suffix != ".md":
        return False
    return bool(_NODE_SUFFIX_RE.fullmatch(path.name))


def remove_generated_relpath(repo_root: Path, rel_path: str) -> int:
    """Remove one generated markdown path and its token sidecar (for page markdown)."""
    removed = 0
    path = repo_root / rel_path
    if path.exists():
        path.unlink()
        removed += 1
    if path.suffix == ".md":
        sidecar = token_sidecar_path(path)
        if sidecar.exists():
            sidecar.unlink()
            removed += 1
    return removed


def prune_file_artifacts_from_manifest(
    state: FigmaSyncState,
    repo_root: Path,
    file_key: str,
    *,
    drop_manifest_entry: bool,
    drop_tracked: bool,
) -> int:
    """Prune all known generated artifacts for one file key from current manifest state."""
    removed = 0
    file_entry = state.manifest.files.get(file_key)
    if file_entry is not None:
        for page in file_entry.pages.values():
            for rel in sorted(entry_paths(page)):
                removed += remove_generated_relpath(repo_root, rel)
        if drop_manifest_entry:
            state.manifest.files.pop(file_key, None)

    if drop_tracked and file_key in state.manifest.tracked_files:
        state.manifest.tracked_files.remove(file_key)

    return removed


def find_generated_orphans(
    repo_root: Path,
    *,
    candidate_dirs: set[Path],
    expected_paths: set[str],
) -> list[str]:
    """Find generated md/tokens files under candidate dirs that are not in expected_paths."""
    orphans: set[str] = set()
    for directory in candidate_dirs:
        if not directory.exists() or not directory.is_dir():
            continue
        for md in directory.glob("*.md"):
            rel = str(md.relative_to(repo_root))
            if is_generated_md_relpath(rel) and rel not in expected_paths:
                orphans.add(rel)
        for tok in directory.glob("*.tokens.json"):
            md_rel = token_sidecar_rel_to_md_rel(str(tok.relative_to(repo_root)))
            if is_generated_md_relpath(md_rel) and md_rel not in expected_paths:
                orphans.add(str(tok.relative_to(repo_root)))
    return sorted(orphans)
