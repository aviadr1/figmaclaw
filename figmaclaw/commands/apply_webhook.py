"""figmaclaw apply-webhook — process Figma FILE_UPDATE / FILE_DELETE webhooks."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import click

from figmaclaw.commands._shared import load_state, require_figma_api_key
from figmaclaw.commands.pull import _git_commit_page
from figmaclaw.figma_client import FigmaClient
from figmaclaw.git_utils import git_push as _git_push
from figmaclaw.prune_utils import prune_file_artifacts_from_manifest
from figmaclaw.pull_logic import pull_file


class WebhookAuthError(Exception):
    """Raised when the webhook passcode does not match FIGMA_WEBHOOK_SECRET."""


@click.command("apply-webhook")
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit after each page.")
@click.option(
    "--push-every",
    "push_every",
    default=10,
    type=int,
    show_default=True,
    help="Push every N commits when --auto-commit is set.",
)
@click.pass_context
def apply_webhook_cmd(ctx: click.Context, auto_commit: bool, push_every: int) -> None:
    """Process a Figma webhook payload from FIGMA_WEBHOOK_PAYLOAD env var."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()

    payload = os.environ.get("FIGMA_WEBHOOK_PAYLOAD", "")
    if not payload:
        raise click.UsageError("FIGMA_WEBHOOK_PAYLOAD environment variable is not set.")

    webhook_secret = os.environ.get("FIGMA_WEBHOOK_SECRET") or None

    try:
        asyncio.run(
            _run(
                api_key=api_key,
                repo_dir=repo_dir,
                payload=payload,
                webhook_secret=webhook_secret,
                auto_commit=auto_commit,
                push_every=push_every,
            )
        )
    except WebhookAuthError as exc:
        raise click.ClickException(str(exc)) from exc


async def _run(
    *,
    api_key: str,
    repo_dir: Path,
    payload: str,
    webhook_secret: str | None,
    auto_commit: bool = False,
    push_every: int = 10,
) -> None:
    data = json.loads(payload)

    # Validate passcode if secret is configured
    if webhook_secret is not None:
        passcode = data.get("passcode", "")
        if passcode != webhook_secret:
            raise WebhookAuthError("Webhook passcode mismatch — rejecting payload.")

    event_type = str(data.get("event_type", "")).upper()
    file_key: str = data.get("file_key") or data.get("file_id") or ""
    if not file_key:
        click.echo("Webhook payload missing file_key/file_id — skipping.")
        return

    state = load_state(repo_dir)

    if event_type == "FILE_DELETE":
        had_file = file_key in state.manifest.files or file_key in state.manifest.tracked_files
        removed_paths = prune_file_artifacts_from_manifest(
            state,
            repo_dir,
            file_key,
            drop_manifest_entry=True,
            drop_tracked=True,
        )
        if had_file:
            state.manifest.skipped_files[file_key] = "deleted via FILE_DELETE webhook"
            state.save()
            click.echo(f"COMMIT_MSG:sync: figmaclaw apply-webhook — file deleted [{file_key}]")
            click.echo(f"{file_key}: pruned {removed_paths} generated path(s) after FILE_DELETE.")
        else:
            click.echo(f"File {file_key!r} is not tracked — skipping.")
        return

    if file_key not in state.manifest.tracked_files:
        click.echo(f"File {file_key!r} is not tracked — skipping.")
        return

    commit_count = 0

    def on_page_written(page_label: str, paths: list[str]) -> None:
        nonlocal commit_count
        if not auto_commit:
            return
        committed = _git_commit_page(repo_dir, page_label)
        if committed:
            commit_count += 1
            click.echo(f"  ✓ committed: {page_label}")
            if push_every and commit_count % push_every == 0:
                click.echo(f"  ↑ pushing ({commit_count} commits)...")
                _git_push(repo_dir)

    async with FigmaClient(api_key) as client:
        result = await pull_file(
            client,
            file_key,
            state,
            repo_dir,
            on_page_written=on_page_written,
        )

    state.save()

    if result.pages_written > 0:
        n = result.pages_written
        click.echo(f"COMMIT_MSG:sync: figmaclaw apply-webhook — {n} page(s) updated [{file_key}]")
    else:
        click.echo(f"{file_key}: no pages changed.")
