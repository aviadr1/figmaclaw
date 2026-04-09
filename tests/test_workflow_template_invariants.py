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
    """INVARIANT: scheduled sync group cannot be canceled by webhook debounce."""
    text = bundled_template_text("figmaclaw-sync.yaml")

    assert "cancel-in-progress: false" in text
    assert "group: figma-git-sync-pull" in text


def test_manage_webhooks_template_is_installed() -> None:
    """INVARIANT: webhook-management stub is part of managed templates."""
    text = bundled_template_text("figmaclaw-manage-webhooks.yaml")

    assert "manage-webhooks.yml@main" in text


def test_concurrency_groups_are_isolated_by_workflow_role() -> None:
    """INVARIANT: webhook debounce cannot cancel scheduled sync runs."""

    def _group(text: str) -> str:
        m = re.search(r"^\s*group:\s*([^\n]+)\s*$", text, flags=re.MULTILINE)
        assert m is not None
        return m.group(1).strip()

    sync_group = _group(bundled_template_text("figmaclaw-sync.yaml"))
    webhook_group = _group(bundled_template_text("figmaclaw-webhook.yaml"))

    assert sync_group == "figma-git-sync-pull"
    assert webhook_group == "figma-git-webhook"
    assert sync_group != webhook_group
