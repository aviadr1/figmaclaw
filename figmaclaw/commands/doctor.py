"""figmaclaw doctor — verify installation, configuration, and connectivity.

Checks everything needed for figmaclaw to work:
  1. CLI installed and importable
  2. FIGMA_API_KEY set and valid (makes a test API call)
  3. Manifest exists and is loadable (if --repo-dir has tracked files)
  4. Figma pages exist on disk
  5. Git repo is accessible
  6. Claude Code CLI available (optional, for enrichment)

Use this after initial setup or when debugging CI failures.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import click

EMPTY_LIST_PAGE_HASH = "4f53cda18c2baa0c"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    """Print a check result and return whether it passed."""
    icon = "\u2713" if ok else "\u2717"
    msg = f"  {icon} {label}"
    if detail:
        msg += f" — {detail}"
    click.echo(msg)
    return ok


@click.command("doctor")
@click.pass_context
def doctor_cmd(ctx: click.Context) -> None:
    """Verify figmaclaw installation, configuration, and connectivity."""
    repo_dir = Path(ctx.obj["repo_dir"])
    passed = 0
    failed = 0
    warnings = 0

    click.echo("figmaclaw doctor\n")

    # 1. Version
    try:
        import figmaclaw._build_info as bi

        ver = bi.__version__
        sha = bi.__commit__[:8] if bi.__commit__ else "unknown"
        if _check("figmaclaw installed", True, f"v{ver} ({sha})"):
            passed += 1
    except Exception as e:
        _check("figmaclaw installed", False, str(e))
        failed += 1

    # 2. Python version
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 12)
    if _check("Python >= 3.12", ok, py):
        passed += 1
    else:
        failed += 1

    # 3. FIGMA_API_KEY
    api_key = os.environ.get("FIGMA_API_KEY", "")
    if api_key:
        # Test the key by fetching user info
        try:
            import httpx

            r = httpx.get(
                "https://api.figma.com/v1/me",
                headers={"X-Figma-Token": api_key},
                timeout=10,
            )
            if r.status_code == 200:
                user = r.json().get("handle", "unknown")
                _check("FIGMA_API_KEY valid", True, f"authenticated as {user}")
                passed += 1
            else:
                _check("FIGMA_API_KEY valid", False, f"HTTP {r.status_code}")
                failed += 1
        except Exception as e:
            _check("FIGMA_API_KEY valid", False, f"connection error: {e}")
            failed += 1
    else:
        _check("FIGMA_API_KEY set", False, "not found in environment")
        failed += 1

    # 4. Repo dir
    if _check("repo directory exists", repo_dir.is_dir(), str(repo_dir)):
        passed += 1
    else:
        failed += 1

    # 5. Git repo
    git_dir = repo_dir / ".git"
    if _check("git repository", git_dir.exists()):
        passed += 1
    else:
        _check("git repository", False, f"no .git in {repo_dir}")
        failed += 1

    # 6. Manifest
    manifest_dir = repo_dir / ".figma-sync"
    manifest_file = manifest_dir / "manifest.json"
    if manifest_file.exists():
        try:
            from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION
            from figmaclaw.figma_sync_state import (
                FigmaSyncState,
                file_has_pull_schema_debt,
                page_schema_is_current,
            )

            state = FigmaSyncState(repo_dir)
            state.load()
            n_files = len(state.manifest.files)
            n_pages = sum(len(f.pages) for f in state.manifest.files.values())
            _check(
                "manifest loadable",
                True,
                f"{n_files} file(s), {n_pages} page(s) tracked",
            )
            passed += 1

            stale_schema_files: list[str] = []
            stale_schema_pages = 0
            for file_entry in state.manifest.files.values():
                if not file_has_pull_schema_debt(
                    file_entry,
                    current_pull_schema_version=CURRENT_PULL_SCHEMA_VERSION,
                    should_skip_page=state.should_skip_page,
                ):
                    continue
                stale_schema_files.append(
                    f"{file_entry.file_name} (v{file_entry.pull_schema_version})"
                )
                stale_schema_pages += sum(
                    1
                    for page_entry in file_entry.pages.values()
                    if not state.should_skip_page(page_entry.page_name)
                    and not page_schema_is_current(
                        file_entry,
                        page_entry,
                        current_pull_schema_version=CURRENT_PULL_SCHEMA_VERSION,
                    )
                )
            if stale_schema_files:
                detail = "; ".join(stale_schema_files[:3])
                if len(stale_schema_files) > 3:
                    detail += f" (+{len(stale_schema_files) - 3} more)"
                _check(
                    "pull schema current",
                    False,
                    f"{len(stale_schema_files)} file(s) below "
                    f"v{CURRENT_PULL_SCHEMA_VERSION}, {stale_schema_pages} page(s) stale: {detail}",
                )
                warnings += 1
            else:
                _check("pull schema current", True, f"v{CURRENT_PULL_SCHEMA_VERSION}")
                passed += 1

            # 6b. Canon PP-1 / partial-pull check (PR 129 H2): a page with md_path=None
            # AND component_md_paths=[] AND not a deliberate skip is the
            # exact "stuck" shape we shipped v8/v9 to fix. If any survive,
            # surface them so the user knows a pull is needed (typically
            # caused by being on an old figmaclaw before the schema bump).
            partial_pulls: list[tuple[str, bool]] = []
            skipped_empty_pages = 0
            for file_entry in state.manifest.files.values():
                for page_entry in file_entry.pages.values():
                    if not page_entry.md_path and not page_entry.component_md_paths:
                        if state.should_skip_page(page_entry.page_name):
                            skipped_empty_pages += 1
                            continue
                        partial_pulls.append(
                            (
                                f"{file_entry.file_name} / {page_entry.page_name}",
                                page_entry.page_hash == EMPTY_LIST_PAGE_HASH,
                            )
                        )
            if partial_pulls:
                sample = [label for label, _empty_hash in partial_pulls[:3]]
                empty_hash_count = sum(1 for _label, empty_hash in partial_pulls if empty_hash)
                detail = "; ".join(sample)
                if len(partial_pulls) > 3:
                    detail += f" (+{len(partial_pulls) - 3} more)"
                if empty_hash_count:
                    detail += f"; {empty_hash_count} with empty-list hash"
                if skipped_empty_pages:
                    detail += f"; {skipped_empty_pages} skipped empty page(s) matched skip_pages"
                _check(
                    "no partial-pull pages",
                    False,
                    f"{len(partial_pulls)} page(s) with md_path=null and "
                    f"component_md_paths=[]: {detail}",
                )
                warnings += 1
            else:
                detail = (
                    f"{skipped_empty_pages} skipped empty page(s) matched skip_pages"
                    if skipped_empty_pages
                    else ""
                )
                _check("no partial-pull pages", True, detail)
                passed += 1
        except Exception as e:
            _check("manifest loadable", False, str(e))
            failed += 1
    else:
        _check(
            "manifest exists",
            False,
            "no .figma-sync/manifest.json — run 'figmaclaw track' first",
        )
        warnings += 1

    # 7. Figma pages on disk
    figma_dir = repo_dir / "figma"
    if figma_dir.exists():
        md_files = list(figma_dir.rglob("*.md"))
        _check("figma pages on disk", True, f"{len(md_files)} .md file(s)")
        passed += 1
    else:
        _check(
            "figma/ directory",
            False,
            "no figma/ — run 'figmaclaw pull' first",
        )
        warnings += 1

    # 8. Workflow files
    wf_dir = repo_dir / ".github" / "workflows"
    wf_files = [
        wf_dir / "figmaclaw-sync.yaml",
        wf_dir / "figmaclaw-webhook.yaml",
    ]
    found_wf = [f.name for f in wf_files if f.exists()]
    missing_wf = [f.name for f in wf_files if not f.exists()]
    if found_wf and not missing_wf:
        _check("CI workflows installed", True, ", ".join(found_wf))
        passed += 1
    elif found_wf:
        _check(
            "CI workflows",
            False,
            f"found: {', '.join(found_wf)}; missing: {', '.join(missing_wf)}",
        )
        warnings += 1
    else:
        _check(
            "CI workflows",
            False,
            "none found — run 'figmaclaw init' to copy templates",
        )
        warnings += 1

    # 9. Claude Code CLI (optional)
    claude_path = shutil.which("claude")
    if claude_path:
        _check("Claude Code CLI", True, claude_path)
        passed += 1
    else:
        _check(
            "Claude Code CLI (optional)",
            False,
            "not found — enrichment won't work without it",
        )
        warnings += 1

    # 10. CLAUDE_CODE_OAUTH_TOKEN (optional)
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        _check("CLAUDE_CODE_OAUTH_TOKEN set", True)
        passed += 1
    else:
        _check(
            "CLAUDE_CODE_OAUTH_TOKEN (optional)",
            False,
            "not set — CI enrichment requires this",
        )
        warnings += 1

    # Summary
    click.echo("")
    click.echo(f"{passed} passed, {failed} failed, {warnings} warnings")
    if failed > 0:
        sys.exit(1)
