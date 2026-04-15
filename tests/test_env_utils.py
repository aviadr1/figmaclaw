from __future__ import annotations

from pathlib import Path

from tests.env_utils import load_repo_dotenv


def test_load_repo_dotenv_loads_missing_env_vars(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("FIGMA_API_KEY", raising=False)
    (tmp_path / ".env").write_text("FIGMA_API_KEY=test-key\n")

    load_repo_dotenv(tmp_path)

    assert __import__("os").environ.get("FIGMA_API_KEY") == "test-key"


def test_load_repo_dotenv_does_not_override_existing_vars(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FIGMA_API_KEY", "already-set")
    (tmp_path / ".env").write_text("FIGMA_API_KEY=from-dotenv\n")

    load_repo_dotenv(tmp_path)

    assert __import__("os").environ.get("FIGMA_API_KEY") == "already-set"
