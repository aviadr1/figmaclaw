"""Tests for commands/diff.py — figmaclaw diff.

Tests use a temporary git repo with figmaclaw-style .md files to verify
that the diff command correctly detects structural design changes and
ignores enrichment-only changes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from figmaclaw.main import cli

# ── Helpers ──────────────────────────────────────────────────────────

_BASIC_PAGE = """\
---
file_key: abc123
page_node_id: '100:1'
frames: ['11:1', '11:2']
flows: [['11:1', '11:2']]
enriched_hash: null
enriched_frame_hashes: {}
---

# Test Page

## Section (`10:1`)

| Name | Node ID | Description |
| --- | --- | --- |
| Welcome | `11:1` | A welcome screen |
| Login | `11:2` | Login form |

## Screen Flow

```mermaid
graph LR
  11:1 --> 11:2
```
"""

_FRAMES_ADDED_PAGE = """\
---
file_key: abc123
page_node_id: '100:1'
frames: ['11:1', '11:2', '11:3', '11:4']
flows: [['11:1', '11:2'], ['11:2', '11:3']]
enriched_hash: null
enriched_frame_hashes: {}
---

# Test Page

## Section (`10:1`)

| Name | Node ID | Description |
| --- | --- | --- |
| Welcome | `11:1` | A welcome screen |
| Login | `11:2` | Login form |
| Dashboard | `11:3` | Main dashboard |
| Settings | `11:4` | Settings panel |

## Screen Flow

```mermaid
graph LR
  11:1 --> 11:2
  11:2 --> 11:3
```
"""

_FRAMES_REMOVED_PAGE = """\
---
file_key: abc123
page_node_id: '100:1'
frames: ['11:1']
flows: []
enriched_hash: null
enriched_frame_hashes: {}
---

# Test Page

## Section (`10:1`)

| Name | Node ID | Description |
| --- | --- | --- |
| Welcome | `11:1` | A welcome screen |
"""

_ENRICHMENT_ONLY_CHANGE = """\
---
file_key: abc123
page_node_id: '100:1'
frames: ['11:1', '11:2']
flows: [['11:1', '11:2']]
enriched_hash: deadbeef12345678
enriched_at: '2026-04-01T12:00:00Z'
enriched_frame_hashes: {'11:1': a3f2b7c1, '11:2': e4d9f8a2}
---

# Test Page

This page shows the onboarding flow with enriched descriptions.

## Section (`10:1`)

| Name | Node ID | Description |
| --- | --- | --- |
| Welcome | `11:1` | A beautifully designed welcome screen with branding |
| Login | `11:2` | Login form with email and password fields |

## Screen Flow

```mermaid
graph LR
  11:1 --> 11:2
```
"""

_RENAMED_FRAME_PAGE = """\
---
file_key: abc123
page_node_id: '100:1'
frames: ['11:1', '11:2']
flows: [['11:1', '11:2']]
enriched_hash: null
enriched_frame_hashes: {}
---

# Test Page

## Section (`10:1`)

| Name | Node ID | Description |
| --- | --- | --- |
| Welcome v2 | `11:1` | A welcome screen |
| Sign In | `11:2` | Login form |
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _init_repo(repo: Path) -> None:
    """Create a git repo with initial config and an initial commit."""
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    # Create a .gitkeep so we can always make an initial commit
    (repo / ".gitkeep").write_text("")


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message, "--allow-empty")


def _backdate_commit(repo: Path, days_ago: int, message: str) -> None:
    """Stage and commit with a date in the past."""
    import os
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    date_str = dt.strftime("%Y-%m-%dT%H:%M:%S %z")
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = date_str
    env["GIT_COMMITTER_DATE"] = date_str
    subprocess.run(
        ["git", "-C", str(repo), "add", "."],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True, capture_output=True, text=True, env=env,
    )


# ── Tests ────────────────────────────────────────────────────────────


def test_new_page_detected(tmp_path: Path) -> None:
    """A new .md file should appear as a new page."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # Initial empty commit
    _backdate_commit(repo, 10, "init")

    # Add a new page recently
    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 2, "add page")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "New Pages" in result.output
    assert "test-page-100-1.md" in result.output


def test_added_frames_detected(tmp_path: Path) -> None:
    """Frames added to the frames: list should be reported."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 10, "initial page")

    # Add frames
    (figma_dir / "test-page-100-1.md").write_text(_FRAMES_ADDED_PAGE)
    _backdate_commit(repo, 2, "add frames")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "Modified Pages" in result.output
    assert "+2 added" in result.output
    assert "11:3" in result.output
    assert "Dashboard" in result.output
    assert "11:4" in result.output


def test_removed_frames_detected(tmp_path: Path) -> None:
    """Frames removed from the frames: list should be reported."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 10, "initial page")

    # Remove a frame
    (figma_dir / "test-page-100-1.md").write_text(_FRAMES_REMOVED_PAGE)
    _backdate_commit(repo, 2, "remove frame")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "-1 removed" in result.output
    assert "11:2" in result.output


def test_flow_changes_detected(tmp_path: Path) -> None:
    """Changes to flows: should be reported."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 10, "initial page")

    # Add frames and flows
    (figma_dir / "test-page-100-1.md").write_text(_FRAMES_ADDED_PAGE)
    _backdate_commit(repo, 2, "add flows")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "+1 new connections" in result.output
    assert "11:3" in result.output


def test_enrichment_only_changes_ignored(tmp_path: Path) -> None:
    """Changes to enriched_hash, enriched_at, body descriptions should NOT appear."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 10, "initial page")

    # Only change enrichment fields and body descriptions
    (figma_dir / "test-page-100-1.md").write_text(_ENRICHMENT_ONLY_CHANGE)
    _backdate_commit(repo, 2, "enrich page")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "No design changes detected." in result.output
    assert "New Pages" not in result.output
    assert "Modified Pages" not in result.output


def test_renamed_frames_detected(tmp_path: Path) -> None:
    """Frame renames (same node_id, different name in body table) should be detected."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 10, "initial page")

    # Rename frames
    (figma_dir / "test-page-100-1.md").write_text(_RENAMED_FRAME_PAGE)
    _backdate_commit(repo, 2, "rename frames")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "2 renamed" in result.output
    assert "Welcome v2" in result.output
    assert "Sign In" in result.output


def test_json_output_format(tmp_path: Path) -> None:
    """--format json should produce valid JSON with expected structure."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 10, "initial page")

    (figma_dir / "test-page-100-1.md").write_text(_FRAMES_ADDED_PAGE)
    _backdate_commit(repo, 2, "add frames")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(repo), "diff", "figma/", "--since", "7d", "--format", "json",
    ])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "since" in data
    assert "until" in data
    assert "modified_pages" in data
    assert "new_pages" in data
    assert len(data["modified_pages"]) == 1
    page = data["modified_pages"][0]
    assert page["file_key"] == "abc123"
    assert len(page["added_frames"]) == 2
    assert any(f["node_id"] == "11:3" for f in page["added_frames"])


def test_json_new_page(tmp_path: Path) -> None:
    """New pages appear in the new_pages list in JSON format."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _backdate_commit(repo, 10, "init")

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 2, "add page")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "--repo-dir", str(repo), "diff", "figma/", "--since", "7d", "--format", "json",
    ])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["new_pages"]) == 1
    assert data["new_pages"][0]["total_frames"] == 2


def test_default_since(tmp_path: Path) -> None:
    """Command works with default --since (7d)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    _backdate_commit(repo, 10, "init")

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 2, "add page")

    runner = CliRunner()
    # No --since flag — should use default 7d
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/"])
    assert result.exit_code == 0, result.output
    assert "New Pages" in result.output


def test_no_changes(tmp_path: Path) -> None:
    """When nothing changed, report says so."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    figma_dir = repo / "figma" / "app" / "pages"
    figma_dir.mkdir(parents=True)
    (figma_dir / "test-page-100-1.md").write_text(_BASIC_PAGE)
    _backdate_commit(repo, 20, "old page")

    runner = CliRunner()
    result = runner.invoke(cli, ["--repo-dir", str(repo), "diff", "figma/", "--since", "7d"])
    assert result.exit_code == 0, result.output
    assert "No design changes detected." in result.output
