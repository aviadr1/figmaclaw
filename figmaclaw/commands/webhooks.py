"""figmaclaw webhooks — manage Figma file-level webhooks for tracked files.

IDEMPOTENCY CONTRACT
====================
The invariant this command group enforces is:

    For every file in .figma-sync/manifest.json tracked_files, there is
    EXACTLY ONE ACTIVE Figma webhook pointing to the configured endpoint.

Subcommands and their contracts:

  sync       -- Enforce the invariant: create missing webhooks, delete
                duplicates (same file + endpoint registered more than once).
                Safe to run repeatedly; produces the same final state.
                This is the command to use in CI and for routine maintenance.

  register   -- Only add webhooks for files that have none for this endpoint.
                Does NOT clean up duplicates. Use for initial setup or when
                you want a conservative "only add, never delete" run.

  validate   -- Check current state against the invariant. Reports:
                  * Missing webhooks (tracked files with no webhook)
                  * Duplicate webhooks (same file + endpoint > 1)
                  * Stale webhooks (pointing to a different endpoint)
                Exits non-zero if any issues found. Use in CI health checks.

  list       -- Print all registered webhooks (across all tracked files).

  delete-all -- Delete all file-level webhooks, optionally filtered to a
                specific endpoint. Use before switching endpoints.

Env vars:
    FIGMA_API_KEY        — Figma personal access token (needs webhooks:write)
    FIGMA_WEBHOOK_SECRET — Passcode sent in every webhook payload for verification
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Protocol

import click

from figmaclaw.commands._shared import load_state, require_figma_api_key
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_models import ValidationReport, Webhook


class WebhookClient(Protocol):
    async def list_file_webhooks(self, file_key: str) -> list[dict[str, Any]]: ...

    async def create_file_webhook(
        self,
        file_key: str,
        endpoint: str,
        passcode: str,
        *,
        event_type: str = "FILE_UPDATE",
        description: str = "figmaclaw sync",
    ) -> dict[str, Any]: ...

    async def delete_webhook(self, webhook_id: str) -> None: ...


# ---------------------------------------------------------------------------
# State loading
# ---------------------------------------------------------------------------


def _load_tracked_files(repo_dir: Path) -> list[str]:
    state = load_state(repo_dir)
    if not state.manifest.tracked_files:
        raise click.ClickException(
            f"No tracked files found in {repo_dir}/.figma-sync/manifest.json"
        )
    return list(state.manifest.tracked_files)


def _require_api_key() -> str:
    return require_figma_api_key()


def _require_passcode() -> str:
    passcode = os.environ.get("FIGMA_WEBHOOK_SECRET", "")
    if not passcode:
        click.echo("Warning: FIGMA_WEBHOOK_SECRET not set — passcode will be empty", err=True)
    return passcode


# ---------------------------------------------------------------------------
# Fetch + group helpers
# ---------------------------------------------------------------------------


async def _fetch_all_webhooks(
    client: WebhookClient,
    file_keys: list[str],
) -> list[Webhook]:
    """Return all webhooks across all tracked files as Webhook models."""
    all_webhooks: list[Webhook] = []
    for file_key in file_keys:
        raw = await client.list_file_webhooks(file_key)
        all_webhooks.extend(Webhook.model_validate(wh) for wh in raw)
    return all_webhooks


def _group_webhooks_by_file(
    webhooks: list[Webhook],
    endpoint: str,
) -> dict[str, list[Webhook]]:
    """Group webhooks by file_key, keeping only those matching *endpoint*."""
    by_file: dict[str, list[Webhook]] = defaultdict(list)
    for wh in webhooks:
        if wh.endpoint == endpoint:
            by_file[wh.context_id].append(wh)
    return by_file


# ---------------------------------------------------------------------------
# Core operations (async, take an injected FigmaClient — easy to test)
# ---------------------------------------------------------------------------


async def validate(
    client: WebhookClient,
    endpoint: str,
    file_keys: list[str],
) -> ValidationReport:
    """Check exactly-one-webhook-per-file invariant and print findings."""
    webhooks = await _fetch_all_webhooks(client, file_keys)
    by_file = _group_webhooks_by_file(webhooks, endpoint)

    report = ValidationReport()
    for file_key in file_keys:
        matches = by_file.get(file_key, [])
        if len(matches) == 0:
            report.missing.append(file_key)
        elif len(matches) > 1:
            report.duplicates.append((file_key, matches))

    tracked = set(file_keys)
    report.stale = [wh for wh in webhooks if wh.endpoint != endpoint and wh.context_id in tracked]

    if report.missing:
        click.echo(f"MISSING ({len(report.missing)} files have no webhook for {endpoint}):")
        for fk in report.missing:
            click.echo(f"  {fk}")
    if report.duplicates:
        click.echo(f"DUPLICATES ({len(report.duplicates)} files have >1 webhook for {endpoint}):")
        for fk, whs in report.duplicates:
            click.echo(f"  {fk}: webhook ids {[wh.id for wh in whs]}")
    if report.stale:
        click.echo(f"STALE ({len(report.stale)} webhooks point to a different endpoint):")
        for wh in report.stale:
            click.echo(f"  {wh.id} ({wh.context_id}) → {wh.endpoint}")
    if report.ok:
        click.echo(f"OK — all {len(file_keys)} files have exactly one webhook → {endpoint}")

    return report


async def _create_missing(
    client: WebhookClient,
    file_keys: list[str],
    by_file: dict[str, list[Webhook]],
    endpoint: str,
    passcode: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Create webhooks for files that have none. Returns (created, failed)."""
    created = failed = 0
    for file_key in file_keys:
        if by_file.get(file_key):
            continue
        if dry_run:
            click.echo(f"  [dry-run] would create {file_key}")
            continue
        try:
            raw = await client.create_file_webhook(file_key, endpoint, passcode)
            wh = Webhook.model_validate(raw)
            click.echo(f"  created {file_key} → webhook id {wh.id}")
            created += 1
        except Exception as exc:  # noqa: BLE001 — report and continue
            click.echo(f"  FAILED to create {file_key}: {exc}")
            failed += 1
    return created, failed


async def sync(
    client: WebhookClient,
    endpoint: str,
    file_keys: list[str],
    *,
    dry_run: bool = False,
) -> None:
    """Enforce exactly-one webhook per tracked file for this endpoint."""
    passcode = _require_passcode()
    webhooks = await _fetch_all_webhooks(client, file_keys)
    by_file = _group_webhooks_by_file(webhooks, endpoint)

    click.echo(f"Syncing webhooks for {len(file_keys)} files → {endpoint}")
    if dry_run:
        click.echo("[dry-run mode — no changes will be made]")

    created, failed = await _create_missing(
        client,
        file_keys,
        by_file,
        endpoint,
        passcode,
        dry_run,
    )
    deleted = 0
    skipped = sum(1 for fk in file_keys if len(by_file.get(fk, [])) == 1)

    # Deduplicate: keep first, delete extras
    for file_key in file_keys:
        matches = by_file.get(file_key, [])
        if len(matches) > 1:
            keep = matches[0]
            extras = matches[1:]
            click.echo(
                f"  {file_key}: duplicate webhooks {[wh.id for wh in extras]} (keeping {keep.id})"
            )
            for wh in extras:
                if dry_run:
                    click.echo(f"    [dry-run] would delete {wh.id}")
                    continue
                try:
                    await client.delete_webhook(wh.id)
                    click.echo(f"    deleted {wh.id}")
                    deleted += 1
                except Exception as exc:  # noqa: BLE001
                    click.echo(f"    FAILED to delete {wh.id}: {exc}")
                    failed += 1

    if dry_run:
        return
    click.echo(
        f"\nDone: {created} created, {deleted} duplicates removed, "
        f"{skipped} already correct, {failed} failed"
    )
    if failed:
        sys.exit(1)


async def register(
    client: WebhookClient,
    endpoint: str,
    file_keys: list[str],
    *,
    dry_run: bool = False,
) -> None:
    """Add webhooks only for files that have none. Never deletes."""
    passcode = _require_passcode()
    webhooks = await _fetch_all_webhooks(client, file_keys)
    by_file = _group_webhooks_by_file(webhooks, endpoint)

    click.echo(f"Registering webhooks for {len(file_keys)} files → {endpoint}")
    if dry_run:
        click.echo("[dry-run mode — no changes will be made]")

    duplicates = [fk for fk in file_keys if len(by_file.get(fk, [])) > 1]
    if duplicates:
        click.echo(f"Warning: {len(duplicates)} files have duplicate webhooks — run 'sync' to fix")

    created, failed = await _create_missing(
        client,
        file_keys,
        by_file,
        endpoint,
        passcode,
        dry_run,
    )
    skipped = sum(1 for fk in file_keys if by_file.get(fk))

    click.echo(f"\nDone: {created} created, {skipped} skipped, {failed} failed")
    if failed:
        sys.exit(1)


async def list_all(client: WebhookClient, file_keys: list[str]) -> list[Webhook]:
    """Print and return all webhooks registered for tracked files."""
    webhooks = await _fetch_all_webhooks(client, file_keys)
    if not webhooks:
        click.echo("No webhooks registered.")
    else:
        click.echo(f"{'ID':<12} {'context_id':<26} {'status':<8} endpoint")
        click.echo("-" * 90)
        for wh in webhooks:
            click.echo(f"{wh.id:<12} {wh.context_id:<26} {wh.status:<8} {wh.endpoint}")
    click.echo(f"\nTotal: {len(webhooks)}")
    return webhooks


async def delete_all(
    client: WebhookClient,
    file_keys: list[str],
    *,
    endpoint_filter: str | None = None,
) -> None:
    webhooks = await _fetch_all_webhooks(client, file_keys)
    to_delete = [wh for wh in webhooks if endpoint_filter is None or wh.endpoint == endpoint_filter]
    if not to_delete:
        click.echo("Nothing to delete.")
        return
    click.echo(f"Deleting {len(to_delete)} webhooks...")
    for wh in to_delete:
        try:
            await client.delete_webhook(wh.id)
            click.echo(f"  {wh.id} ({wh.context_id}) → deleted")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  {wh.id} ({wh.context_id}) → FAILED: {exc}")


# ---------------------------------------------------------------------------
# Click command group
# ---------------------------------------------------------------------------


@click.group("webhooks", help=__doc__)
def webhooks_group() -> None:
    """Manage Figma file-level webhooks for tracked files."""


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@webhooks_group.command("sync")
@click.option("--endpoint", required=True, help="Webhook delivery endpoint URL.")
@click.option("--dry-run", is_flag=True, help="Preview changes without mutating Figma.")
@click.pass_context
def sync_cmd(ctx: click.Context, endpoint: str, dry_run: bool) -> None:
    """Enforce exactly-one webhook per tracked file (idempotent)."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = _require_api_key()
    file_keys = _load_tracked_files(repo_dir)

    async def _main() -> None:
        async with FigmaClient(api_key) as client:
            await sync(client, endpoint, file_keys, dry_run=dry_run)

    _run(_main())


@webhooks_group.command("register")
@click.option("--endpoint", required=True, help="Webhook delivery endpoint URL.")
@click.option("--dry-run", is_flag=True, help="Preview changes without mutating Figma.")
@click.pass_context
def register_cmd(ctx: click.Context, endpoint: str, dry_run: bool) -> None:
    """Add missing webhooks only (conservative, never deletes)."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = _require_api_key()
    file_keys = _load_tracked_files(repo_dir)

    async def _main() -> None:
        async with FigmaClient(api_key) as client:
            await register(client, endpoint, file_keys, dry_run=dry_run)

    _run(_main())


@webhooks_group.command("validate")
@click.option("--endpoint", required=True, help="Webhook delivery endpoint URL.")
@click.pass_context
def validate_cmd(ctx: click.Context, endpoint: str) -> None:
    """Check invariant; exits non-zero if issues are found."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = _require_api_key()
    file_keys = _load_tracked_files(repo_dir)

    async def _main() -> ValidationReport:
        async with FigmaClient(api_key) as client:
            return await validate(client, endpoint, file_keys)

    report = _run(_main())
    if not report.ok:
        sys.exit(1)


@webhooks_group.command("list")
@click.pass_context
def list_cmd(ctx: click.Context) -> None:
    """List all registered webhooks for tracked files."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = _require_api_key()
    file_keys = _load_tracked_files(repo_dir)

    async def _main() -> None:
        async with FigmaClient(api_key) as client:
            await list_all(client, file_keys)

    _run(_main())


@webhooks_group.command("delete-all")
@click.option("--endpoint", default=None, help="Only delete webhooks pointing to this endpoint.")
@click.pass_context
def delete_all_cmd(ctx: click.Context, endpoint: str | None) -> None:
    """Delete all file-level webhooks (optionally filtered to an endpoint)."""
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = _require_api_key()
    file_keys = _load_tracked_files(repo_dir)

    async def _main() -> None:
        async with FigmaClient(api_key) as client:
            await delete_all(client, file_keys, endpoint_filter=endpoint)

    _run(_main())
