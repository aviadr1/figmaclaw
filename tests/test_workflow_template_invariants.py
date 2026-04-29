"""Invariants for managed workflow templates."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import yaml

from figmaclaw.workflow_templates import bundled_template_text


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


def test_variables_reusable_workflow_replays_generated_catalog_on_push_conflict() -> None:
    """INVARIANT: variables commits survive concurrent census/enrichment pushes.

    The variables job can commit many file-scope catalog refreshes while census
    or enrichment jobs also push. Recovery after a rejected push must replay the
    deterministic variables refresh on the newest remote branch instead of
    text-merging generated ``ds_catalog.json`` content, which can conflict.
    """

    text = (Path(__file__).parents[1] / ".github" / "workflows" / "variables.yml").read_text(
        encoding="utf-8"
    )

    assert "PUSH_STATUS=$?" in text
    assert 'if [ "$PUSH_STATUS" -eq 0 ]; then' in text
    assert 'git reset --hard "origin/${{ inputs.target_ref }}"' in text
    assert text.count("figmaclaw variables") >= 2
    assert 'git pull --no-rebase origin "${{ inputs.target_ref }}" && git push' not in text
    assert (
        'git pull --no-rebase --ff-only origin "${{ inputs.target_ref }}" && git push' not in text
    )


def test_variables_push_replay_branch_executes_under_bash_errexit(tmp_path: Path) -> None:
    """INVARIANT: rejected-push replay is executable under GitHub's bash -e wrapper.

    The linear-git run 25098005988 proved that string-level workflow checks were
    not enough: the first rejected ``git push`` stopped the Push step before the
    generated-output replay branch ran. This test executes the actual Push step
    body with fake ``git``/``figmaclaw`` binaries and requires the replay path.
    """

    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github" / "workflows" / "variables.yml").read_text(
            encoding="utf-8"
        )
    )
    push_script = next(
        step["run"] for step in workflow["jobs"]["variables"]["steps"] if step.get("name") == "Push"
    )
    push_script = (
        push_script.replace("${{ inputs.target_ref }}", "test/figmaclaw-pr-129-ci")
        .replace("${{ inputs.require_authoritative }}", "false")
        .replace("${{ inputs.file_key }}", "")
        .replace("${{ inputs.variables_source }}", "auto")
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "trace.log"
    push_state = tmp_path / "push-count"

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
        "figmaclaw variables --source auto --auto-commit",
        "git push 2",
    ]


def test_variables_workflows_can_require_authoritative_definitions() -> None:
    """INVARIANT: CI can fail loudly when only unavailable markers exist."""

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
