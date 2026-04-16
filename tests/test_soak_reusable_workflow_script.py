"""Invariants for reusable workflow soak script guardrails."""

from __future__ import annotations

from pathlib import Path


def test_soak_script_exists_and_is_executable() -> None:
    script = Path("scripts/soak_reusable_workflow.sh")
    assert script.exists()
    assert script.stat().st_mode & 0o111


def test_soak_script_scans_for_known_failure_patterns() -> None:
    script = Path("scripts/soak_reusable_workflow.sh")
    text = script.read_text(encoding="utf-8")

    required_patterns = (
        "PHANTOM SELECTION",
        "unsupported enrichment log schema/header",
        "The following paths are ignored by one of your",
        "NO-PROGRESS",
        "STUCK:",
    )
    for pattern in required_patterns:
        assert pattern in text

    assert "gh run watch" in text
    assert "gh run view" in text
