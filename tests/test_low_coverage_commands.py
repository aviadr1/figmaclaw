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


def _suggest_fixture_with_libraries(
    tmp_path: Path,
) -> tuple[Path, SimpleNamespace, object]:
    """Variant of the suggest fixture with multi-library catalog stub —
    used to exercise --library filtering behavior (#133)."""
    sidecar = tmp_path / "page.tokens.json"
    sidecar.write_text(
        json.dumps(
            {
                "file_key": "abc123",
                "frames": {
                    "11:1": {
                        "name": "Login Screen",
                        "issues": [
                            {
                                "property": "fill",
                                "current_value": {"r": 1},
                                "hex": "#FFFFFF",
                                "count": 1,
                            },
                        ],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Two libraries: TAP IN (target) and OLD DS (should be filterable away).
    # Each has one variable with the same hex, to prove the filter
    # actually narrows candidates.
    catalog = SimpleNamespace(
        libraries={
            "local:tap-in-hash": SimpleNamespace(name="TAP IN DESIGN SYSTEM"),
            "local:old-ds-hash": SimpleNamespace(name="OLD_Gigaverse Design System"),
        },
        variables={
            "vid_tap_white": SimpleNamespace(
                values_by_mode={"_default": SimpleNamespace(hex="#FFFFFF", numeric_value=None)},
                library_hash="local:tap-in-hash",
            ),
            "vid_old_white": SimpleNamespace(
                values_by_mode={"_default": SimpleNamespace(hex="#FFFFFF", numeric_value=None)},
                library_hash="local:old-ds-hash",
            ),
        },
    )

    # Fake suggester that records which library_hashes it received.
    received: dict[str, set[str] | None] = {"library_hashes": None}

    def fake_suggest(work_sidecar: dict, _catalog: SimpleNamespace, library_hashes=None) -> None:
        received["library_hashes"] = library_hashes
        # Mimic real matcher: filter candidates by the allowlist if one was given.
        for frame in work_sidecar.get("frames", {}).values():
            for issue in frame.get("issues", []):
                hex_ = issue.get("hex")
                cands = []
                if hex_ == "#FFFFFF":
                    for vid, entry in _catalog.variables.items():
                        if library_hashes is None or entry.library_hash in library_hashes:
                            cands.append(vid)
                if len(cands) == 1:
                    issue["suggest_status"] = "auto"
                    issue["candidates"] = cands
                    issue["fix_variable_id"] = cands[0]
                elif len(cands) > 1:
                    issue["suggest_status"] = "ambiguous"
                    issue["candidates"] = cands
                else:
                    issue["suggest_status"] = "no_match"
                    issue["candidates"] = []

    # Attach the recorder onto the callable so tests can inspect it.
    fake_suggest.received = received  # type: ignore[attr-defined]
    return sidecar, catalog, fake_suggest


def test_suggest_tokens_library_filter_narrows_candidates(tmp_path: Path) -> None:
    """INVARIANT (#133, audit-log F1): --library limits matches to listed
    libraries, so migration audits don't get OLD-DS suggestions."""
    sidecar, catalog, fake_suggest = _suggest_fixture_with_libraries(tmp_path)

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        # Without --library: both white-variants match → ambiguous.
        unfiltered = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "suggest-tokens", "--sidecar", str(sidecar)],
            catch_exceptions=False,
        )
        assert unfiltered.exit_code == 0
        out = sidecar.parent / "page.suggestions.json"
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["frames"]["11:1"]["issues"][0]["suggest_status"] == "ambiguous"
        assert set(data["frames"]["11:1"]["issues"][0]["candidates"]) == {
            "vid_tap_white",
            "vid_old_white",
        }
        out.unlink()  # reset for the next invocation

    with p1, p2, p3, p4:
        # With --library tap: only TAP IN variant matches → auto.
        filtered = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "suggest-tokens",
                "--sidecar",
                str(sidecar),
                "--library",
                "tap",
            ],
            catch_exceptions=False,
        )
    assert filtered.exit_code == 0
    assert "Library filter: 1 libraries" in filtered.output
    assert "TAP IN DESIGN SYSTEM" in filtered.output
    assert "OLD_Gigaverse" not in filtered.output

    data = json.loads((sidecar.parent / "page.suggestions.json").read_text(encoding="utf-8"))
    issue = data["frames"]["11:1"]["issues"][0]
    assert issue["suggest_status"] == "auto"
    assert issue["candidates"] == ["vid_tap_white"]
    assert issue["fix_variable_id"] == "vid_tap_white"


def test_suggest_tokens_library_filter_no_match_errors(tmp_path: Path) -> None:
    """If --library matches no library, fail loudly rather than silently
    proceed with an empty filter (which would mark every issue no_match)."""
    sidecar, catalog, fake_suggest = _suggest_fixture_with_libraries(tmp_path)

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "suggest-tokens",
                "--sidecar",
                str(sidecar),
                "--library",
                "nonexistent-library-name",
            ],
            catch_exceptions=False,
        )
    assert result.exit_code != 0
    assert "matched no libraries" in result.output


def test_suggest_tokens_library_filter_matches_by_hash(tmp_path: Path) -> None:
    """--library can match either by name OR by library_hash key."""
    sidecar, catalog, fake_suggest = _suggest_fixture_with_libraries(tmp_path)

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "suggest-tokens",
                "--sidecar",
                str(sidecar),
                "--library",
                "tap-in-hash",  # substring of the library_hash key, not the name
            ],
            catch_exceptions=False,
        )
    assert result.exit_code == 0
    assert "TAP IN DESIGN SYSTEM" in result.output


def _suggest_fixture(tmp_path: Path) -> tuple[Path, SimpleNamespace, object]:
    """Common fixture for suggest-tokens tests: writes a sidecar, returns
    (sidecar_path, catalog_stub, fake_suggest_callable)."""
    sidecar = tmp_path / "page.tokens.json"
    sidecar.write_text(
        json.dumps(
            {
                "file_key": "abc123",
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
                },
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

    def fake_suggest(work_sidecar: dict, _catalog: SimpleNamespace, library_hashes=None) -> None:
        del library_hashes  # not used by this fixture
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

    return sidecar, catalog, fake_suggest


def _patch_catalog_loaders(catalog: object, fake_suggest: object):
    """Helper: patch out catalog load + suggest computation + staleness check."""
    return (
        patch("figmaclaw.commands.suggest_tokens.load_catalog", return_value=catalog),
        patch("figmaclaw.commands.suggest_tokens.suggest_for_sidecar", side_effect=fake_suggest),
        patch("figmaclaw.commands.suggest_tokens.catalog_staleness_errors", return_value=[]),
        patch("figmaclaw.commands.suggest_tokens.load_state", return_value=MagicMock()),
    )


def test_suggest_tokens_does_not_mutate_sidecar(tmp_path: Path) -> None:
    """INVARIANT (#133): suggest-tokens must never modify the input sidecar.

    The sidecar is a CI-managed artifact regenerated by `pull`; mutations get
    silently reverted by the next CI run. Suggestions belong in a sibling file.
    """
    sidecar, catalog, fake_suggest = _suggest_fixture(tmp_path)
    sidecar_bytes_before = sidecar.read_bytes()

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "suggest-tokens", "--sidecar", str(sidecar)],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert sidecar.read_bytes() == sidecar_bytes_before, (
        "suggest-tokens mutated the sidecar — see #133"
    )


def test_suggest_tokens_default_output_is_sibling_suggestions_json(tmp_path: Path) -> None:
    """Default output: ``foo.tokens.json`` → sibling ``foo.suggestions.json``."""
    sidecar, catalog, fake_suggest = _suggest_fixture(tmp_path)

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
            cli,
            ["--repo-dir", str(tmp_path), "suggest-tokens", "--sidecar", str(sidecar)],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    expected = sidecar.parent / "page.suggestions.json"
    assert expected.exists(), f"expected {expected} to exist; output:\n{result.output}"
    assert "Wrote suggestions" in result.output and "page.suggestions.json" in result.output

    written = json.loads(expected.read_text(encoding="utf-8"))
    login_issues = written["frames"]["11:1"]["issues"]
    assert login_issues[0]["suggest_status"] == "auto"
    assert login_issues[1]["suggest_status"] == "ambiguous"
    assert written["frames"]["11:2"]["issues"][0]["suggest_status"] == "no_match"
    assert written["suggested_at"] == "2026-04-15T12:00:00Z"


def test_suggest_tokens_custom_output_path(tmp_path: Path) -> None:
    sidecar, catalog, fake_suggest = _suggest_fixture(tmp_path)
    custom = tmp_path / "out" / "elsewhere.json"

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "suggest-tokens",
                "--sidecar",
                str(sidecar),
                "--output",
                str(custom),
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert custom.exists()
    # Sibling default path must NOT exist when --output overrides it.
    assert not (sidecar.parent / "page.suggestions.json").exists()
    written = json.loads(custom.read_text(encoding="utf-8"))
    assert written["frames"]["11:1"]["issues"][0]["suggest_status"] == "auto"


def test_suggest_tokens_output_dash_writes_stdout(tmp_path: Path) -> None:
    sidecar, catalog, fake_suggest = _suggest_fixture(tmp_path)

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "suggest-tokens",
                "--sidecar",
                str(sidecar),
                "--output",
                "-",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    # No file written.
    assert not (sidecar.parent / "page.suggestions.json").exists()
    # Stdout must contain a parseable JSON document with the suggestions.
    # Click captures stdout; the printed table comes first, then the JSON.
    out = result.output
    json_start = out.index("{\n")
    parsed = json.loads(out[json_start:])
    assert parsed["frames"]["11:1"]["issues"][0]["suggest_status"] == "auto"


def test_suggest_tokens_dry_run_writes_nothing(tmp_path: Path) -> None:
    sidecar, catalog, fake_suggest = _suggest_fixture(tmp_path)
    sidecar_bytes_before = sidecar.read_bytes()

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
            cli,
            [
                "--repo-dir",
                str(tmp_path),
                "suggest-tokens",
                "--sidecar",
                str(sidecar),
                "--dry-run",
            ],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert sidecar.read_bytes() == sidecar_bytes_before
    assert not (sidecar.parent / "page.suggestions.json").exists()
    # Summary table still printed.
    assert "Catalog: 2 variables" in result.output
    assert "auto:" in result.output
    # No "Wrote" message.
    assert "Wrote suggestions" not in result.output


def test_suggest_tokens_frame_filter_outputs_only_processed_frames(tmp_path: Path) -> None:
    """Frame-filtered runs produce filtered output — they don't merge back into
    a hidden full sidecar (which the original mutating implementation did)."""
    sidecar, catalog, fake_suggest = _suggest_fixture(tmp_path)

    runner = CliRunner()
    p1, p2, p3, p4 = _patch_catalog_loaders(catalog, fake_suggest)
    with p1, p2, p3, p4:
        result = runner.invoke(
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

    assert result.exit_code == 0
    written = json.loads((sidecar.parent / "page.suggestions.json").read_text(encoding="utf-8"))
    assert "11:1" in written["frames"]
    assert "11:2" not in written["frames"]
    assert written["frames"]["11:1"]["issues"][0]["suggest_status"] == "auto"


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


def test_self_update_runs_uv_tool_install_force(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], check: bool) -> None:
        calls.append(command)
        assert check is True

    monkeypatch.setattr("figmaclaw.commands.self_cmd.subprocess.run", fake_run)

    result = CliRunner().invoke(
        self_group,
        ["update", "--source", "/tmp/figmaclaw"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert calls == [
        ["uv", "tool", "install", "--force", "--reinstall", "--upgrade", "/tmp/figmaclaw"]
    ]


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
