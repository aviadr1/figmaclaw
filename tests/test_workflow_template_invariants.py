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


def test_webhook_template_debounces_with_isolated_group() -> None:
    """INVARIANT: webhook debounce uses a dedicated concurrency group."""
    text = bundled_template_text("figmaclaw-webhook.yaml")

    assert "cancel-in-progress: true" in text
    assert "group: figma-git-webhook" in text


def test_sync_template_isolated_from_webhook_cancellation() -> None:
    """INVARIANT: sync job is serialized and insulated from webhook debounce."""
    text = bundled_template_text("figmaclaw-sync.yaml")

    assert "sync:\n" in text
    assert "group: figma-git-sync-pull" in text
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
    assert "group: figma-git-variables" in text


def test_concurrency_groups_are_isolated_by_workflow_role() -> None:
    """INVARIANT: concurrency groups are explicit and non-overlapping by role."""

    sync_text = bundled_template_text("figmaclaw-sync.yaml")
    webhook_text = bundled_template_text("figmaclaw-webhook.yaml")

    sync_groups = set(re.findall(r"^\s*group:\s*([^\n]+)\s*$", sync_text, flags=re.MULTILINE))
    webhook_groups = set(re.findall(r"^\s*group:\s*([^\n]+)\s*$", webhook_text, flags=re.MULTILINE))

    assert "figma-git-sync-pull" in sync_groups
    assert "figma-git-census" in sync_groups
    assert "figma-git-variables" in sync_groups
    assert "figma-git-enrich" in sync_groups
    assert "figma-git-enrich-large" in sync_groups
    assert webhook_groups == {"figma-git-webhook"}
    assert sync_groups.isdisjoint(webhook_groups)


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

        assert "PUSH_STATUS=$?" in text
        assert 'if [ "$PUSH_STATUS" -eq 0 ]; then' in text
        assert 'git reset --hard "origin/${{ inputs.target_ref || github.ref_name }}"' in text
        assert "This is safe only because this GitHub runner has no human edits" in text
        assert text.count(command) >= 2
        assert "git push ||" not in text
        assert 'git pull --no-rebase origin "${{ inputs.target_ref }}" && git push' not in text
        assert (
            'git pull --no-rebase --ff-only origin "${{ inputs.target_ref }}" && git push'
            not in text
        )


def test_registry_push_replay_branch_executes_under_bash_errexit(tmp_path: Path) -> None:
    """INVARIANT WF-1: rejected-push replay is executable under GitHub's bash -e wrapper.

    The linear-git run 25098005988 proved that string-level workflow checks were
    not enough: the first rejected ``git push`` stopped the Push step before the
    generated-output replay branch ran. This test executes the actual Push step
    bodies with fake ``git``/``figmaclaw`` binaries and requires the replay path.
    """

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    git_bin = bin_dir / "git"
    git_bin.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "$1" = "push" ]; then
              count=0
              if [ -f "$PUSH_STATE" ]; then
                count="$(cat "$PUSH_STATE")"
              fi
              count=$((count + 1))
              echo "$count" > "$PUSH_STATE"
              echo "git push $count" >> "$TRACE_FILE"
              if [ "$count" -eq 1 ]; then
                exit 1
              fi
              exit 0
            fi
            echo "git $*" >> "$TRACE_FILE"
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

    cases = (
        (
            "variables.yml",
            "variables",
            {
                "${{ inputs.target_ref || github.ref_name }}": "test/figmaclaw-pr-129-ci",
                "${{ inputs.require_authoritative }}": "false",
                "${{ inputs.file_key }}": "",
                "${{ inputs.variables_source }}": "auto",
            },
            "figmaclaw variables --source auto --auto-commit",
        ),
        (
            "census.yml",
            "census",
            {
                "${{ inputs.target_ref || github.ref_name }}": "test/figmaclaw-pr-129-ci",
                "${{ inputs.file_key }}": "",
            },
            "figmaclaw census --auto-commit",
        ),
    )

    for workflow_name, job_name, replacements, expected_replay_command in cases:
        push_script = _reusable_push_script(workflow_name, job_name)
        for old, new in replacements.items():
            push_script = push_script.replace(old, new)

        trace = tmp_path / f"{workflow_name}.trace.log"
        push_state = tmp_path / f"{workflow_name}.push-count"
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["TRACE_FILE"] = str(trace)
        env["PUSH_STATE"] = str(push_state)

        result = subprocess.run(
            ["bash", "-e", "-c", push_script],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert trace.read_text(encoding="utf-8").splitlines() == [
            "git push 1",
            "git fetch origin test/figmaclaw-pr-129-ci",
            "git reset --hard origin/test/figmaclaw-pr-129-ci",
            expected_replay_command,
            "git push 2",
        ]


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


def test_version_bump_is_not_a_post_merge_main_mutation() -> None:
    """INVARIANT: version bumps are PR contents, not bot pushes to protected main."""

    assert not (Path(__file__).parents[1] / ".github" / "workflows" / "bump-version.yml").exists()
