"""figmaclaw census — snapshot published component sets for tracked Figma files.

Calls GET /v1/files/{file_key}/component_sets (one request per file) and writes
a `_census.md` file alongside each file's pages/ directory. The file is only
rewritten when the content hash changes, keeping diffs meaningful.

Output: figma/{file_slug}/_census.md

Frontmatter fields:
  file_key          — Figma file key
  generated_at      — ISO 8601 UTC timestamp of this run
  content_hash      — 16-char stable hash of sorted (name, key) pairs.
                      Changes when components are added, removed, or renamed.
                      Unchanged when component content changes (thumbnail, etc.)
  component_set_count — total published component sets in this file

Body: markdown table of all published component sets, sorted alphabetically.
Columns: Component set | Key | Page | Updated

Why content hash not timestamp:
  The hash lets CI and agents detect "did the DS registry change?" separately
  from "did anything in the file get touched?". Only registry changes (add /
  remove / rename a component set) should trigger re-audit. The generated_at
  timestamp covers staleness checking independently.

Hash skip:
  If the existing _census.md already has `content_hash: {current_hash}` in its
  frontmatter, the file is not rewritten and no commit is emitted. Only real
  registry changes trigger a write + commit, keeping git history signal-rich.

Integration:
  Run after `figmaclaw pull` in CI:
    figmaclaw census --auto-commit
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml

from figmaclaw.commands._shared import load_state, require_figma_api_key, require_tracked_files
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_paths import census_path, file_slug_for_key
from figmaclaw.git_utils import git_commit

# ── Hash ─────────────────────────────────────────────────────────────────────


def _compute_hash(component_sets: list[dict[str, Any]]) -> str:
    """Stable 16-char hash of the published component set registry.

    Hashes sorted (name, key) pairs only. Changes when a component set is
    added, removed, renamed, or its Figma key changes. Does NOT change when
    component content (thumbnail, internal variants) changes.
    """
    pairs = sorted(
        (cs.get("name", ""), cs.get("key", ""))
        for cs in component_sets
        if cs.get("name") and cs.get("key")
    )
    canonical = json.dumps(pairs, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render(
    file_key: str,
    file_name: str,
    component_sets: list[dict[str, Any]],
    content_hash: str,
    generated_at: str,
) -> str:
    """Render the census markdown content."""
    sorted_sets = sorted(component_sets, key=lambda cs: cs.get("name", "").lower())

    fm: dict[str, Any] = {
        "file_key": file_key,
        "generated_at": generated_at,
        "content_hash": content_hash,
        "component_set_count": len(sorted_sets),
    }
    # Sort keys for stable diffs; use block style (no flow) since values are all scalars.
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=True).strip()

    lines: list[str] = [f"---\n{fm_yaml}\n---", ""]
    lines.append(f"# {file_name} — Published Component Sets\n")
    lines.append("| Component set | Key | Page | Updated |")
    lines.append("|---|---|---|---|")

    for cs in sorted_sets:
        name = cs.get("name", "")
        key = cs.get("key", "")
        containing = cs.get("containing_frame", {})
        page = containing.get("pageName", "—")
        updated_raw = cs.get("updated_at", "")
        updated = updated_raw[:10] if updated_raw else "—"
        lines.append(f"| `{name}` | `{key}` | {page} | {updated} |")

    return "\n".join(lines) + "\n"


# ── Hash check (fast, no YAML parse needed) ───────────────────────────────────


def _existing_hash(path: Path) -> str | None:
    """Extract content_hash from existing _census.md frontmatter, or None."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("content_hash:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


# ── Command ───────────────────────────────────────────────────────────────────


@click.command("census")
@click.option(
    "--file-key",
    "file_key",
    default=None,
    help="Census only this file key (default: all tracked files).",
)
@click.option(
    "--auto-commit", "auto_commit", is_flag=True, help="git commit each written _census.md."
)
@click.option("--force", is_flag=True, help="Write even if content hash is unchanged.")
@click.pass_context
def census_cmd(
    ctx: click.Context,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
) -> None:
    """Snapshot published component sets for all tracked files.

    Writes figma/{file-slug}/_census.md for each file that has published
    component sets. Only updates the file when the component registry changes.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    api_key = require_figma_api_key()

    asyncio.run(_run(api_key, repo_dir, file_key, auto_commit, force))


async def _run(
    api_key: str,
    repo_dir: Path,
    file_key: str | None,
    auto_commit: bool,
    force: bool,
) -> None:
    state = load_state(repo_dir)
    if not require_tracked_files(state):
        return

    keys = [file_key] if file_key else list(state.manifest.tracked_files)
    written: list[str] = []

    async with FigmaClient(api_key) as client:
        for key in keys:
            if key not in state.manifest.tracked_files:
                click.echo(f"{key}: not tracked — skip")
                continue

            skip_reason = state.manifest.skipped_files.get(key)
            if skip_reason:
                click.echo(f"{key}: skipped — {skip_reason}")
                continue

            file_entry = state.manifest.files.get(key)
            file_name = file_entry.file_name if file_entry else key
            file_slug = file_slug_for_key(file_name, key)

            try:
                component_sets = await client.get_component_sets(key)
            except Exception as exc:
                click.echo(f"{key} ({file_name}): failed — {exc}")
                continue

            if not component_sets:
                # No published component sets — skip silently (e.g. product files)
                continue

            content_hash = _compute_hash(component_sets)
            out_path = repo_dir / census_path(file_slug)

            if not force and _existing_hash(out_path) == content_hash:
                click.echo(f"{file_name}: census unchanged (hash {content_hash})")
                continue

            generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            content = _render(key, file_name, component_sets, content_hash, generated_at)

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")

            # Enforce the round-trip invariant: _existing_hash must be able to read back
            # the hash we just embedded. If this fires, _render and _existing_hash have
            # drifted (e.g. the frontmatter field was renamed) and every subsequent run
            # will rewrite the file, creating spurious git commits.
            assert _existing_hash(out_path) == content_hash, (
                f"BUG: wrote {out_path} but _existing_hash cannot recover content_hash={content_hash!r}. "
                "The census skip check is broken — every future run will rewrite this file."
            )

            rel = census_path(file_slug)
            click.echo(
                f"{file_name}: wrote {len(component_sets)} component set(s)"
                f" [hash {content_hash}] → {rel}"
            )
            written.append(rel)

            if auto_commit:
                committed = git_commit(repo_dir, [rel], f"sync: figmaclaw census — {file_name}")
                if committed:
                    click.echo("  ✓ committed")

    if written:
        click.echo(f"COMMIT_MSG:sync: figmaclaw census — {len(written)} file(s) updated")
