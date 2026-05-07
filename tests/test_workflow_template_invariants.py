"""Invariants for managed workflow templates."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import yaml

from figmaclaw.workflow_templates import bundled_template_text

REPO_ROOT = Path(__file__).parents[1]


def _reusable_workflow_text(name: str) -> str:
    return (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _reusable_push_script(workflow_name: str, job_name: str) -> str:
    workflow = yaml.safe_load(_reusable_workflow_text(workflow_name))
    return next(
        step["run"] for step in workflow["jobs"][job_name]["steps"] if step.get("name") == "Push"
    )


def _reusable_step_script(workflow_name: str, job_name: str, step_name: str) -> str:
    workflow = yaml.safe_load(_reusable_workflow_text(workflow_name))
    return next(
        step["run"] for step in workflow["jobs"][job_name]["steps"] if step.get("name") == step_name
    )


def _publisher_script_text() -> str:
    return (REPO_ROOT / "scripts" / "publish_generated_registry.sh").read_text(encoding="utf-8")


def test_webhook_template_debounces_with_isolated_group() -> None:
    """INVARIANT WF-5: webhook debounce must not cancel Claude enrichment."""
    text = bundled_template_text("figmaclaw-webhook.yaml")
    workflow = yaml.safe_load(text)

    assert "concurrency" not in workflow
    assert "cancel-in-progress: true" in text
    assert "group: figma-git-webhook-apply-${{ github.ref }}" in text


def test_sync_template_isolated_from_webhook_cancellation() -> None:
    """INVARIANT: sync job is serialized and insulated from webhook debounce."""
    text = bundled_template_text("figmaclaw-sync.yaml")

    assert "sync:\n" in text
    assert "group: figma-git-sync-pull-${{ github.ref }}" in text
    assert "cancel-in-progress: false" in text
    assert "group: figma-git-webhook" not in text


def test_sync_template_threads_figmaclaw_ref_to_reusable_jobs() -> None:
    """INVARIANT: consumer repos can run a figmaclaw PR branch in real CI."""
    text = bundled_template_text("figmaclaw-sync.yaml")

    assert "figmaclaw_ref:" in text
    assert text.count("figmaclaw_ref: ${{ github.event.inputs.figmaclaw_ref || 'main' }}") == 5


def test_host_templates_do_not_repeat_target_ref_boilerplate() -> None:
    """INVARIANT WF-3: caller branch refresh is owned by reusable workflows."""

    for template_name in (
        "figmaclaw-sync.yaml",
        "figmaclaw-webhook.yaml",
        "figmaclaw-variables.yaml",
    ):
        assert "target_ref:" not in bundled_template_text(template_name)


def test_sync_template_uses_reusable_stateful_jobs() -> None:
    """INVARIANT: consumer repos call upstream reusable jobs, not local logic."""
    text = bundled_template_text("figmaclaw-sync.yaml")

    assert "uses: aviadr1/figmaclaw/.github/workflows/sync.yml@main" in text
    assert "uses: aviadr1/figmaclaw/.github/workflows/census.yml@main" in text
    assert "uses: aviadr1/figmaclaw/.github/workflows/variables.yml@main" in text
    assert "uses: aviadr1/figmaclaw/.github/workflows/claude-run.yml@main" in text


def test_variables_template_threads_current_branch_to_reusable_workflow() -> None:
    """INVARIANT: standalone variables dispatch uses upstream reusable workflow."""
    text = bundled_template_text("figmaclaw-variables.yaml")

    assert "uses: aviadr1/figmaclaw/.github/workflows/variables.yml@main" in text
    assert "target_ref:" not in text


def test_webhook_template_uses_reusable_stateful_jobs() -> None:
    """INVARIANT: webhook caller stays a thin reusable-workflow stub."""
    text = bundled_template_text("figmaclaw-webhook.yaml")

    assert "uses: aviadr1/figmaclaw/.github/workflows/webhook.yml@main" in text
    assert "uses: aviadr1/figmaclaw/.github/workflows/claude-run.yml@main" in text
    assert "target_ref:" not in text


def test_reusable_stateful_workflows_default_target_ref_to_caller_branch() -> None:
    """INVARIANT WF-3: called workflows refresh the caller branch by default."""

    for workflow_name in (
        "sync.yml",
        "webhook.yml",
        "census.yml",
        "variables.yml",
        "claude-run.yml",
    ):
        text = _reusable_workflow_text(workflow_name)

        assert "target_ref:" in text
        assert "default: ''" in text
        assert "${{ inputs.target_ref || github.ref_name }}" in text
        assert 'origin "${{ inputs.target_ref }}"' not in text


def test_claude_run_refreshes_target_ref_before_selecting_work() -> None:
    """INVARIANT WF-3: enrichment must not select work from a stale dispatch snapshot."""
    text = _reusable_workflow_text("claude-run.yml")

    assert "target_ref:" in text
    assert (
        'git pull --no-rebase --ff-only origin "${{ inputs.target_ref || github.ref_name }}"'
        in text
    )
    assert text.index("name: Pull latest changes") < text.index("          figmaclaw claude-run \\")


def test_manage_webhooks_template_is_installed() -> None:
    """INVARIANT: webhook-management stub is part of managed templates."""
    text = bundled_template_text("figmaclaw-manage-webhooks.yaml")

    assert "manage-webhooks.yml@main" in text


def test_variables_template_is_installed() -> None:
    """INVARIANT: variables-catalog stub is part of managed templates."""
    text = bundled_template_text("figmaclaw-variables.yaml")

    assert "variables.yml@main" in text
    assert "group: figma-git-variables-${{ github.ref }}" in text


def test_concurrency_groups_are_isolated_by_workflow_role() -> None:
    """INVARIANT: concurrency groups are explicit and non-overlapping by role."""

    sync_text = bundled_template_text("figmaclaw-sync.yaml")
    webhook_text = bundled_template_text("figmaclaw-webhook.yaml")

    sync_groups = set(re.findall(r"^\s*group:\s*([^\n]+)\s*$", sync_text, flags=re.MULTILINE))
    webhook_groups = set(re.findall(r"^\s*group:\s*([^\n]+)\s*$", webhook_text, flags=re.MULTILINE))

    assert "figma-git-sync-pull-${{ github.ref }}" in sync_groups
    assert "figma-git-census-${{ github.ref }}" in sync_groups
    assert "figma-git-variables-${{ github.ref }}" in sync_groups
    assert "figma-git-enrich-publisher-${{ github.ref }}" in sync_groups
    assert "figma-git-enrich" not in sync_groups
    assert "figma-git-enrich-large" not in sync_groups
    assert webhook_groups == {"figma-git-webhook-apply-${{ github.ref }}"}
    assert sync_groups.isdisjoint(webhook_groups)


def test_concurrency_groups_are_branch_scoped() -> None:
    """INVARIANT WF-7: branch-local writers must not cancel other branches."""

    templates = {
        "figmaclaw-sync.yaml": {
            "figma-git-sync-pull-${{ github.ref }}",
            "figma-git-census-${{ github.ref }}",
            "figma-git-variables-${{ github.ref }}",
            "figma-git-enrich-publisher-${{ github.ref }}",
        },
        "figmaclaw-webhook.yaml": {"figma-git-webhook-apply-${{ github.ref }}"},
        "figmaclaw-variables.yaml": {"figma-git-variables-${{ github.ref }}"},
    }

    for template_name, expected_groups in templates.items():
        text = bundled_template_text(template_name)
        groups = set(re.findall(r"^\s*group:\s*([^\n]+)\s*$", text, flags=re.MULTILINE))
        assert groups == expected_groups
        for group in groups:
            assert "${{ github.ref }}" in group


def test_registry_jobs_queue_instead_of_canceling_marination() -> None:
    """WF-7/WF-8: scheduled registry jobs must not kill in-flight PR marination."""

    sync_text = bundled_template_text("figmaclaw-sync.yaml")
    variables_text = bundled_template_text("figmaclaw-variables.yaml")

    for job_name in ("census", "variables"):
        block = re.search(rf"(?ms)^  {job_name}:\n.*?(?=^  [a-zA-Z_-]+:|\Z)", sync_text)
        assert block is not None
        assert "cancel-in-progress: false" in block.group(0)

    assert "group: figma-git-variables-${{ github.ref }}" in variables_text
    assert "cancel-in-progress: false" in variables_text


def test_claude_publishers_are_serialized_and_not_cancelled() -> None:
    """INVARIANT WF-5: expensive authored enrichment must not race itself."""

    reusable = _reusable_workflow_text("claude-run.yml")
    sync_text = bundled_template_text("figmaclaw-sync.yaml")
    webhook = yaml.safe_load(bundled_template_text("figmaclaw-webhook.yaml"))

    assert "group: claude-run-${{ github.ref }}-${{ inputs.target }}" in reusable
    assert "cancel-in-progress: false" in reusable
    assert "git reset --hard" not in reusable
    assert "figmaclaw-rescue/" in reusable
    assert "Preserve unpublished authored commits" in reusable

    assert sync_text.count("group: figma-git-enrich-publisher-${{ github.ref }}") == 2
    assert sync_text.count("cancel-in-progress: false") >= 3
    assert "figma-git-enrich-large" not in sync_text

    assert "concurrency" not in webhook
    assert webhook["jobs"]["apply-webhook"]["concurrency"] == {
        "group": "figma-git-webhook-apply-${{ github.ref }}",
        "cancel-in-progress": True,
    }


def test_reusable_registry_workflows_replay_generated_artifacts_on_push_conflict() -> None:
    """INVARIANT WF-1: registry commits survive concurrent generated pushes.

    Variables and census jobs both commit deterministic file-scope registries
    while other jobs may also push. Recovery after a rejected push must replay
    deterministic generation on the newest remote branch instead of text-merging
    generated cache snapshots.
    """

    for workflow_name, command in (
        ("variables.yml", "figmaclaw variables"),
        ("census.yml", "figmaclaw census"),
    ):
        text = _reusable_workflow_text(workflow_name)

        assert "Checkout generated-registry publisher" in text
        assert "scripts/publish_generated_registry.sh" in text
        assert "PUBLISH_PROTECTED_PATH_RE:" in text
        assert "REPLAY_COMMAND:" in text
        assert "bash .figmaclaw-workflow/scripts/publish_generated_registry.sh" in text
        assert text.count(command) >= 2
        assert "git push ||" not in text
        assert 'git pull --no-rebase origin "${{ inputs.target_ref }}" && git push' not in text
        assert (
            'git pull --no-rebase --ff-only origin "${{ inputs.target_ref }}" && git push'
            not in text
        )

    publisher = _publisher_script_text()
    assert "MAX_PUBLISH_ATTEMPTS" in publisher
    assert "remote_touched_protected_path" in publisher
    assert 'git rebase "origin/${TARGET_REF}"' in publisher
    assert 'git reset --hard "origin/${TARGET_REF}"' in publisher
    assert 'bash -c "$REPLAY_COMMAND"' in publisher
    assert "This is safe only because this GitHub runner has no human edits" in publisher


def _write_publisher_fake_bins(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    git_bin = bin_dir / "git"
    git_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            case "${1:-}" in
              fetch)
                echo "git fetch ${*:2}" >> "$TRACE_FILE"
                ;;
              rev-list)
                echo "${LOCAL_COMMIT_COUNT:-1}"
                ;;
              push)
                count=0
                if [ -f "$PUSH_STATE" ]; then
                  count="$(cat "$PUSH_STATE")"
                fi
                count=$((count + 1))
                echo "$count" > "$PUSH_STATE"
                echo "git push $count" >> "$TRACE_FILE"
                if [ "$count" -le "${FAIL_PUSHES:-1}" ]; then
                  exit 1
                fi
                ;;
              diff)
                echo "git diff ${*:2}" >> "$TRACE_FILE"
                printf "%b" "${REMOTE_DIFF:-}"
                ;;
              reset)
                echo "git reset ${*:2}" >> "$TRACE_FILE"
                ;;
              rebase)
                echo "git rebase ${*:2}" >> "$TRACE_FILE"
                ;;
              *)
                echo "git $*" >> "$TRACE_FILE"
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    git_bin.chmod(0o755)

    figmaclaw_bin = bin_dir / "figmaclaw"
    figmaclaw_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            echo "figmaclaw $*" >> "$TRACE_FILE"
            """
        ),
        encoding="utf-8",
    )
    figmaclaw_bin.chmod(0o755)

    return bin_dir


def _run_publisher_script(
    tmp_path: Path,
    *,
    protected_re: str = r"^\.figma-sync/ds_catalog\.json$",
    replay_command: str = "figmaclaw variables --source auto --auto-commit",
    remote_diff: str = "",
    local_commits: str = "1",
    fail_pushes: str = "1",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = _write_publisher_fake_bins(tmp_path)
    trace = tmp_path / "publisher.trace.log"
    push_state = tmp_path / "publisher.push-count"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["TRACE_FILE"] = str(trace)
    env["PUSH_STATE"] = str(push_state)
    env["TARGET_REF"] = "test/figmaclaw-pr-143-ci"
    env["PUBLISH_PROTECTED_PATH_RE"] = protected_re
    env["REPLAY_COMMAND"] = replay_command
    env["REMOTE_DIFF"] = remote_diff
    env["LOCAL_COMMIT_COUNT"] = local_commits
    env["FAIL_PUSHES"] = fail_pushes

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "publish_generated_registry.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    lines = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
    return result, lines


def test_generated_registry_publisher_replays_protected_remote_moves(tmp_path: Path) -> None:
    """INVARIANT WF-8: protected registry movement resets and replays generation."""

    result, trace = _run_publisher_script(tmp_path, remote_diff=".figma-sync/ds_catalog.json\\n")

    assert result.returncode == 0, result.stderr + result.stdout
    assert trace == [
        "git fetch origin test/figmaclaw-pr-143-ci",
        "git push 1",
        "git fetch origin test/figmaclaw-pr-143-ci",
        "git diff --name-only HEAD...origin/test/figmaclaw-pr-143-ci",
        "git fetch origin test/figmaclaw-pr-143-ci",
        "git reset --hard origin/test/figmaclaw-pr-143-ci",
        "figmaclaw variables --source auto --auto-commit",
        "git fetch origin test/figmaclaw-pr-143-ci",
        "git push 2",
    ]
    assert "Remote touched protected registry path" in result.stdout


def test_generated_registry_publisher_rebases_disjoint_remote_moves(tmp_path: Path) -> None:
    """INVARIANT WF-8: unrelated remote movement rebases instead of replaying."""

    result, trace = _run_publisher_script(tmp_path, remote_diff="figma/app/pages/home.md\\n")

    assert result.returncode == 0, result.stderr + result.stdout
    assert trace == [
        "git fetch origin test/figmaclaw-pr-143-ci",
        "git push 1",
        "git fetch origin test/figmaclaw-pr-143-ci",
        "git diff --name-only HEAD...origin/test/figmaclaw-pr-143-ci",
        "git rebase origin/test/figmaclaw-pr-143-ci",
        "git fetch origin test/figmaclaw-pr-143-ci",
        "git push 2",
    ]
    assert not any(line.startswith("figmaclaw ") for line in trace)
    assert "Remote did not touch protected registry path" in result.stdout


def test_registry_noop_push_guard_exits_without_push(tmp_path: Path) -> None:
    """INVARIANT WF-4: no-op generated registry jobs must not publish stale heads."""

    result, trace = _run_publisher_script(tmp_path, local_commits="0")

    assert result.returncode == 0, result.stderr + result.stdout
    assert trace == ["git fetch origin test/figmaclaw-pr-143-ci"]
    assert "No local generated registry commits to publish." in result.stdout


def test_claude_unpublished_commit_preservation_executes(tmp_path: Path) -> None:
    """INVARIANT WF-5: rejected authored work is preserved before the runner exits."""

    script = _reusable_step_script("claude-run.yml", "run", "Preserve unpublished authored commits")
    script = script.replace("${{ inputs.target_ref || github.ref_name }}", "feature/pr")
    script = script.replace("${{ github.run_id }}", "12345")
    script = script.replace("${{ github.run_attempt }}", "2")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    git_bin = bin_dir / "git"
    git_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "$1" = "rev-list" ]; then
              echo "2"
              exit 0
            fi
            echo "git $*" >> "$TRACE_FILE"
            """
        ),
        encoding="utf-8",
    )
    git_bin.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    for outcome, expected_status in (("failure", 0), ("success", 1)):
        trace = tmp_path / f"claude-preserve-{outcome}.trace.log"
        case_env = env.copy()
        case_env["TRACE_FILE"] = str(trace)
        case_script = script.replace("${{ steps.claude.outcome }}", outcome)

        result = subprocess.run(
            ["bash", "-e", "-c", case_script],
            cwd=tmp_path,
            env=case_env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == expected_status, result.stderr + result.stdout
        assert trace.read_text(encoding="utf-8").splitlines() == [
            "git fetch origin feature/pr",
            "git push origin HEAD:refs/heads/figmaclaw-rescue/feature-pr/12345-2",
        ]
        assert "Preserving 2 unpublished claude-run commit(s)" in result.stdout


def test_variables_workflows_can_require_authoritative_definitions() -> None:
    """INVARIANT AUTH-1: CI can fail loudly when only unavailable markers exist."""

    reusable = (Path(__file__).parents[1] / ".github" / "workflows" / "variables.yml").read_text(
        encoding="utf-8"
    )
    installed = bundled_template_text("figmaclaw-variables.yaml")

    assert "require_authoritative:" in reusable
    assert "--require-authoritative" in reusable
    assert "require_authoritative:" in installed
    assert "require_authoritative: ${{ github.event.inputs.require_authoritative || false }}" in (
        installed
    )


def test_registry_workflows_receive_team_id_for_fast_noop_paths() -> None:
    """ERR-2: host skeletons pass team context to reusable registry jobs."""

    sync_text = bundled_template_text("figmaclaw-sync.yaml")
    variables_text = bundled_template_text("figmaclaw-variables.yaml")
    census_reusable = _reusable_workflow_text("census.yml")
    variables_reusable = _reusable_workflow_text("variables.yml")

    assert sync_text.count("figma_team_id: ${{ vars.FIGMA_TEAM_ID }}") == 3
    assert "figma_team_id: ${{ vars.FIGMA_TEAM_ID }}" in variables_text
    assert "--team-id" in census_reusable
    assert "figmaclaw census --team-id" in census_reusable
    assert "--team-id" in variables_reusable
    assert "figmaclaw variables --team-id" in variables_reusable


def test_ci_requires_source_controlled_version_bump_in_pr() -> None:
    """INVARIANT: version bumps are PR contents, not bot pushes to protected main."""

    assert not (Path(__file__).parents[1] / ".github" / "workflows" / "bump-version.yml").exists()

    ci_text = _reusable_workflow_text("ci.yml")
    workflow = yaml.safe_load(ci_text)
    test_steps = workflow["jobs"]["test"]["steps"]
    steps = {step["name"]: step for step in test_steps}

    assert "bump-version" not in workflow["jobs"]
    assert workflow["permissions"]["contents"] == "read"
    assert steps["Checkout"]["with"]["fetch-depth"] == 0
    assert steps["Enforce PR version bump"]["if"] == "github.event_name == 'pull_request'"
    assert steps["Enforce PR version bump"]["env"] == {
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}"
    }
    assert (
        steps["Enforce PR version bump"]["run"]
        == 'python3 scripts/check_version_bump.py --base-ref "$BASE_SHA"'
    )
