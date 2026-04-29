from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from figmaclaw import git_utils
from figmaclaw.commands.self_cmd import self_group
from figmaclaw.commands.track import _run as track_run
from figmaclaw.figma_sync_state import FigmaSyncState
from figmaclaw.main import cli


def test_mark_stale_clears_enrichment_and_can_commit(tmp_path: Path) -> None:
    md = tmp_path / "figma" / "web-app" / "pages" / "p.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        """---
file_key: abc123
page_node_id: "1:1"
frames: ["2:2"]
flows: [["2:2", "2:3"]]
enriched_hash: oldhash
enriched_at: '2026-01-01T00:00:00Z'
enriched_frame_hashes: {"2:2": aa}
---
# Body\n\nreal prose\n""",
        encoding="utf-8",
    )

    runner = CliRunner()
    with patch("figmaclaw.commands.mark_stale.git_commit", return_value=True) as gc:
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "mark-stale", str(md), "--auto-commit"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    text = md.read_text(encoding="utf-8")
    assert "enriched_hash" not in text
    assert "enriched_at" not in text
    assert "enriched_frame_hashes" not in text
    assert "# Body" in text
    gc.assert_called_once()
    assert "committed:" in result.output


def test_mark_stale_handles_already_stale_and_no_frontmatter(tmp_path: Path) -> None:
    stale = tmp_path / "stale.md"
    stale.write_text(
        """---
file_key: abc123
page_node_id: "1:1"
frames: ["2:2"]
enriched_schema_version: 0
---
Body\n""",
        encoding="utf-8",
    )
    plain = tmp_path / "plain.md"
    plain.write_text("# not figmaclaw\n", encoding="utf-8")

    runner = CliRunner()
    res1 = runner.invoke(
        cli, ["--repo-dir", str(tmp_path), "mark-stale", str(stale)], catch_exceptions=False
    )
    assert res1.exit_code == 0
    assert "already not enriched" in res1.output

    res2 = runner.invoke(cli, ["--repo-dir", str(tmp_path), "mark-stale", str(plain)])
    assert res2.exit_code == 2
    assert "no figmaclaw frontmatter" in res2.output


def test_suggest_tokens_dry_run_and_frame_filtered_write(tmp_path: Path) -> None:
    sidecar = tmp_path / "page.tokens.json"
    sidecar.write_text(
        json.dumps(
            {
                "frames": {
                    "11:1": {
                        "name": "Login Screen",
                        "issues": [
                            {
                                "property": "fill",
                                "current_value": {"r": 1},
                                "hex": "#FF0000",
                                "count": 2,
                            },
                            {"property": "gap", "current_value": 8.0, "count": 3},
                        ],
                    },
                    "11:2": {
                        "name": "Settings",
                        "issues": [
                            {"property": "radius", "current_value": 12, "count": 1},
                        ],
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Catalog stub uses the v2 values_by_mode shape (canon TC-4).
    catalog = SimpleNamespace(
        variables={
            "a": SimpleNamespace(
                values_by_mode={"_default": SimpleNamespace(hex="#FFFFFF", numeric_value=None)}
            ),
            "b": SimpleNamespace(
                values_by_mode={"_default": SimpleNamespace(hex=None, numeric_value=8)}
            ),
        }
    )

    def fake_suggest(work_sidecar: dict, _catalog: object) -> None:
        for frame in work_sidecar.get("frames", {}).values():
            for issue in frame.get("issues", []):
                prop = issue.get("property")
                if prop == "fill":
                    issue["suggest_status"] = "auto"
                elif prop == "gap":
                    issue["suggest_status"] = "ambiguous"
                else:
                    issue["suggest_status"] = "no_match"
        work_sidecar["suggested_at"] = "2026-04-15T12:00:00Z"

    runner = CliRunner()
    with (
        patch("figmaclaw.commands.suggest_tokens.load_catalog", return_value=catalog),
        patch("figmaclaw.commands.suggest_tokens.suggest_for_sidecar", side_effect=fake_suggest),
    ):
        dry = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "suggest-tokens", "--sidecar", str(sidecar), "--dry-run"],
            catch_exceptions=False,
        )
        assert dry.exit_code == 0
        assert "Catalog: 2 variables (1 color, 1 numeric)" in dry.output
        assert "auto:" in dry.output and "2" in dry.output
        assert "ambiguous:" in dry.output and "3" in dry.output
        assert "no_match:" in dry.output and "1" in dry.output
        after_dry = json.loads(sidecar.read_text(encoding="utf-8"))
        assert "suggest_status" not in json.dumps(after_dry)

        write = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "suggest-tokens",
                "--sidecar",
                str(sidecar),
                "--frame",
                "login",
            ],
            catch_exceptions=False,
        )
        assert write.exit_code == 0
        assert "Wrote updated sidecar" in write.output

    written = json.loads(sidecar.read_text(encoding="utf-8"))
    login_issues = written["frames"]["11:1"]["issues"]
    assert login_issues[0]["suggest_status"] == "auto"
    assert login_issues[1]["suggest_status"] == "ambiguous"
    # Filtered write should not alter non-matching frame
    assert "suggest_status" not in written["frames"]["11:2"]["issues"][0]
    assert written["suggested_at"] == "2026-04-15T12:00:00Z"


def test_suggest_tokens_refuses_stale_catalog(tmp_path: Path) -> None:
    """INVARIANT (CR-2, TC-7): suggest-tokens exits before using stale catalog data."""
    state = FigmaSyncState(tmp_path)
    state.add_tracked_file("abc123", "Design System")
    state.set_file_meta(
        "abc123",
        version="v2",
        last_modified="2026-04-28T00:00:00Z",
        last_checked_at="2026-04-28T00:00:00Z",
        file_name="Design System",
    )
    state.save()

    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "libraries": {
                    "libabc": {
                        "name": "Design System",
                        "source_file_key": "abc123",
                        "source_version": "v1",
                    }
                },
                "variables": {},
            }
        ),
        encoding="utf-8",
    )
    sidecar = tmp_path / "page.tokens.json"
    sidecar.write_text(
        json.dumps({"file_key": "abc123", "frames": {}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["--repo-dir", str(tmp_path), "suggest-tokens", "--sidecar", str(sidecar)],
    )

    assert result.exit_code != 0
    assert "ds_catalog.json is stale" in result.output
    assert "figmaclaw variables --file-key abc123" in result.output


def test_suggest_tokens_accepts_catalog_newer_than_manifest(tmp_path: Path) -> None:
    """A standalone variables refresh can observe a newer Figma version than manifest state."""
    state = FigmaSyncState(tmp_path)
    state.add_tracked_file("abc123", "Design System")
    state.set_file_meta(
        "abc123",
        version="100",
        last_modified="2026-04-28T00:00:00Z",
        last_checked_at="2026-04-28T00:00:00Z",
        file_name="Design System",
    )
    state.save()

    catalog_path = tmp_path / ".figma-sync" / "ds_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "libraries": {
                    "local:abc123": {
                        "name": "Design System",
                        "source_file_key": "abc123",
                        "source_version": "101",
                    }
                },
                "variables": {},
            }
        ),
        encoding="utf-8",
    )
    sidecar = tmp_path / "page.tokens.json"
    sidecar.write_text(
        json.dumps({"file_key": "abc123", "frames": {}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        ["--repo-dir", str(tmp_path), "suggest-tokens", "--sidecar", str(sidecar), "--dry-run"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Catalog:" in result.output


def test_self_skill_list_print_specific_and_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "alpha.md").write_text("alpha body\n", encoding="utf-8")
    (skills_dir / "beta.md").write_text("beta body\n", encoding="utf-8")
    monkeypatch.setattr("figmaclaw.commands.self_cmd._SKILLS_DIR", skills_dir)

    runner = CliRunner()
    listed = runner.invoke(self_group, ["skill", "--list"], catch_exceptions=False)
    assert listed.exit_code == 0
    assert "alpha" in listed.output and "beta" in listed.output

    one = runner.invoke(self_group, ["skill", "alpha"], catch_exceptions=False)
    assert one.exit_code == 0
    assert one.output == "alpha body\n"

    missing = runner.invoke(self_group, ["skill", "missing"])
    assert missing.exit_code != 0
    assert "not found" in missing.output


@pytest.mark.asyncio
async def test_track_run_updates_state_and_optional_pull(tmp_path: Path) -> None:
    client = MagicMock()
    client.get_file_meta = AsyncMock(return_value=SimpleNamespace(name="Web App"))

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("figmaclaw.commands.track.FigmaClient", return_value=ctx),
        patch(
            "figmaclaw.commands.track.pull_file",
            AsyncMock(
                return_value=SimpleNamespace(pages_written=1, md_paths=["figma/web-app/pages/p.md"])
            ),
        ) as pull,
    ):
        await track_run("api-key", tmp_path, "abc123", no_pull=True)
        pull.assert_not_awaited()

        await track_run("api-key", tmp_path, "abc123", no_pull=False)
        pull.assert_awaited()

    state = FigmaSyncState(tmp_path)
    state.load()
    assert "abc123" in state.manifest.tracked_files
    assert state.manifest.files["abc123"].file_name == "Web App"


def test_track_cmd_requires_api_key(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["--repo-dir", str(tmp_path), "track", "abc123"], env={"FIGMA_API_KEY": ""}
    )
    assert result.exit_code != 0
    assert "FIGMA_API_KEY" in result.output


def test_git_utils_commit_and_push_retry(tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    class R:
        def __init__(self, rc: int):
            self.returncode = rc

    def fake_run(cmd: list[str], check: bool = False):  # noqa: ARG001
        recorded.append(cmd)
        if cmd[-1] == "push":
            return R(1 if sum(1 for c in recorded if c[-1] == "push") == 1 else 0)
        if cmd[-3:] == ["diff", "--cached", "--quiet"]:
            return R(1)
        return R(0)

    with patch("subprocess.run", side_effect=fake_run):
        committed = git_utils.git_commit(tmp_path, ["a.md"], "msg")
        assert committed is True
        git_utils.git_push(tmp_path)

    # commit path: add -> diff -> commit
    assert any("add" in cmd and "a.md" in cmd for cmd in recorded)
    assert any("commit" in cmd for cmd in recorded)
    # push retry path: push -> pull --no-rebase -> push
    push_cmds = [cmd for cmd in recorded if cmd[-1] == "push"]
    assert len(push_cmds) == 2
    assert any(cmd[-2:] == ["pull", "--no-rebase"] for cmd in recorded)


def test_git_utils_commit_returns_false_on_no_diff(tmp_path: Path) -> None:
    class R:
        def __init__(self, rc: int):
            self.returncode = rc

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool = False):  # noqa: ARG001
        calls.append(cmd)
        if cmd[-3:] == ["diff", "--cached", "--quiet"]:
            return R(0)
        return R(0)

    with patch("subprocess.run", side_effect=fake_run):
        committed = git_utils.git_commit(tmp_path, ["a.md"], "msg")

    assert committed is False
    assert not any(cmd[-1] == "commit" for cmd in calls)


def test_mark_stale_migrates_missing_enrichment_schema_version(tmp_path: Path) -> None:
    md = tmp_path / "legacy.md"
    md.write_text(
        """---
file_key: abc123
page_node_id: "1:1"
frames: ["2:2"]
---
Body
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    res = runner.invoke(cli, ["--repo-dir", str(tmp_path), "mark-stale", str(md)])
    assert res.exit_code == 0
    assert "cleared enrichment state" in res.output
    assert "enriched_schema_version: 0" in md.read_text(encoding="utf-8")
