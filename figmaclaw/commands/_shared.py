"""Shared command helpers for figmaclaw command modules."""

from __future__ import annotations

import os
from pathlib import Path

import click

from figmaclaw.figma_sync_state import FigmaSyncState

FIGMA_API_KEY_ENV = "FIGMA_API_KEY"
FIGMA_VARIABLES_TOKEN_ENV = "FIGMA_VARIABLES_TOKEN"
FIGMA_WEBHOOK_SECRET_ENV = "FIGMA_WEBHOOK_SECRET"
FIGMA_WEBHOOK_PAYLOAD_ENV = "FIGMA_WEBHOOK_PAYLOAD"


def require_figma_api_key() -> str:
    """Return FIGMA_API_KEY or raise a consistent usage error."""
    api_key = os.environ.get(FIGMA_API_KEY_ENV, "")
    if not api_key:
        raise click.UsageError(f"{FIGMA_API_KEY_ENV} environment variable is not set.")
    return api_key


def figma_variables_api_key(fallback_api_key: str) -> str:
    """Return the token used for Figma Variables endpoints.

    ``FIGMA_API_KEY`` in older installations is often a content-read token
    without Figma's ``file_variables:read`` scope. Let operators provide a
    narrower, variables-capable token without changing the token used for the
    rest of the sync pipeline.
    """
    return os.environ.get(FIGMA_VARIABLES_TOKEN_ENV, "") or fallback_api_key


def load_state(repo_dir: Path) -> FigmaSyncState:
    """Load and return Figma sync state for this repo."""
    state = FigmaSyncState(repo_dir)
    state.load()
    return state


def require_tracked_files(state: FigmaSyncState) -> bool:
    """Return False with a standard message when no tracked files exist."""
    if state.manifest.tracked_files:
        return True
    click.echo("No tracked files. Run 'figmaclaw track <file-key>' first.")
    return False
