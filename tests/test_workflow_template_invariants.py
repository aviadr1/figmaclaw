"""Invariants for managed workflow templates."""

from __future__ import annotations

import re

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


def test_concurrency_groups_are_isolated_by_workflow_role() -> None:
    """INVARIANT: concurrency groups are explicit and non-overlapping by role."""

    sync_text = bundled_template_text("figmaclaw-sync.yaml")
    webhook_text = bundled_template_text("figmaclaw-webhook.yaml")

    sync_groups = set(re.findall(r"^\s*group:\s*([^\n]+)\s*$", sync_text, flags=re.MULTILINE))
    webhook_groups = set(re.findall(r"^\s*group:\s*([^\n]+)\s*$", webhook_text, flags=re.MULTILINE))

    assert "figma-git-sync-pull" in sync_groups
    assert "figma-git-census" in sync_groups
    assert "figma-git-enrich" in sync_groups
    assert "figma-git-enrich-large" in sync_groups
    assert webhook_groups == {"figma-git-webhook"}
    assert sync_groups.isdisjoint(webhook_groups)
