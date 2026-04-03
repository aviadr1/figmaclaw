"""figmaclaw claude-run — launch Claude Code for file enrichment in CI.

Thin orchestrator: discovers files, filters by enrichment status, then
invokes ``claude -p`` for each file with a prompt template.

Single file  → one claude -p invocation
Directory    → one claude -p per file (sequential, commit+push after each)

Prompt template placeholders:
  {file_path}     single-file path
  {file_content}  single-file content
  {filename}      bare filename
  {file_list}     newline-separated list of paths  (directory mode)
  {target_dir}    directory being processed         (directory mode)
"""

from __future__ import annotations

import importlib.resources
import subprocess
import sys
import threading
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# File discovery helpers (no Figma API calls — pure file reads)
# ---------------------------------------------------------------------------


def _changed_files(base: Path, glob_pattern: str) -> list[Path]:
    """Git-modified + untracked files under *base* matching *glob_pattern*."""
    changed: set[str] = set()
    for cmd in [
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        changed.update(r.stdout.splitlines())

    result = []
    for rel in changed:
        p = Path(rel)
        try:
            p.relative_to(base)
        except ValueError:
            continue
        if p.exists() and p.match(glob_pattern):
            result.append(p)
    return sorted(result)


def enrichment_info(md_path: Path) -> tuple[bool, int]:
    """Fast check: does *md_path* need enrichment?

    Returns ``(needs_it, frame_count)``.

    Reads the file directly — no subprocess, no Figma API.  Checks:

    * Has ``enriched_hash`` in frontmatter?  → already enriched → skip.
    * Counts body table rows for a frame-size estimate.
    """
    try:
        text = md_path.read_text()
    except OSError:
        return False, 0

    # Fast frontmatter check — enriched files have this field
    if "enriched_hash:" in (text.split("\n---")[0] if "\n---" in text else ""):
        return False, 0

    # Count frames from body table rows (| name | `node_id` | desc |)
    frame_count = 0
    for line in text.splitlines():
        if line.startswith("| ") and "`" in line and "Node ID" not in line and "---" not in line:
            frame_count += 1

    return True, frame_count


def collect_files(
    target: Path,
    glob_pattern: str,
    changed_only: bool,
    needs_enrichment: bool = False,
    min_frames: int = 0,
    max_frames: int = 0,
) -> list[Path]:
    """Discover files to process, optionally filtering by enrichment status.

    When *needs_enrichment* is True, files are filtered by ``enriched_hash``
    and optionally by frame count (*min_frames* / *max_frames*).  This enables
    two-pass CI enrichment:

    * **Bulk pass** (``--max-frames 80``): many small pages per run.
    * **Large-page pass** (``--min-frames 81 --max-files 1``): one large page
      gets the full CI timeout.
    """
    if target.is_file():
        return [target]
    if changed_only:
        files = _changed_files(target, glob_pattern)
    else:
        files = sorted(target.glob(glob_pattern))
    if needs_enrichment:
        before = len(files)
        enrichable: list[tuple[Path, int]] = []
        skipped_small = 0
        skipped_big = 0
        for f in files:
            needs_it, frame_count = enrichment_info(f)
            if not needs_it:
                continue
            if min_frames > 0 and frame_count < min_frames:
                skipped_small += 1
                continue
            if max_frames > 0 and frame_count > max_frames:
                skipped_big += 1
                continue
            enrichable.append((f, frame_count))
        # Sort smallest first — enrich many small files before hitting big ones
        enrichable.sort(key=lambda x: x[1])
        files = [f for f, _ in enrichable]
        msg = f"[claude-run] {len(files)}/{before} files need enrichment"
        parts = []
        if skipped_small:
            parts.append(f"{skipped_small} below {min_frames} frames")
        if skipped_big:
            parts.append(f"{skipped_big} above {max_frames} frames")
        if parts:
            msg += f" ({', '.join(parts)})"
        click.echo(msg, err=True)
    else:
        click.echo(f"[claude-run] {len(files)} files to process", err=True)
    return files


def build_prompt(template: str, target: Path, files: list[Path]) -> str:
    """Fill template placeholders for a single file."""
    file_path = files[0] if files else target
    content = file_path.read_text() if file_path.exists() else ""
    file_list = "\n".join(f"- {f}" for f in files)
    return (
        template
        .replace("{file_path}", str(file_path))
        .replace("{file_content}", content)
        .replace("{filename}", file_path.name)
        .replace("{file_list}", file_list)
        .replace("{target_dir}", str(target))
    )


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def _run_claude(
    prompt: str,
    model: str,
    max_turns: int,
    skip_permissions: bool,
    extra_flags: list[str],
) -> int:
    """Invoke ``claude -p`` and stream output to stdout/stderr."""
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model,
        "--max-turns", str(max_turns),
        *extra_flags,
    ]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    proc.stdin.write(prompt.encode())
    proc.stdin.close()

    def _relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()

    t = threading.Thread(target=_relay_stderr, daemon=True)
    t.start()

    for line in proc.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    t.join()
    return proc.wait()


# ---------------------------------------------------------------------------
# Bundled prompt resolution
# ---------------------------------------------------------------------------


def default_prompt_path() -> Path:
    """Return the path to the bundled ``figma-batch-enrich.md`` prompt."""
    return Path(
        str(importlib.resources.files("figmaclaw.prompts").joinpath("figma-batch-enrich.md"))
    )


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("claude-run")
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--prompt-file", type=click.Path(exists=True, path_type=Path), default=None,
    help="Prompt template file. Defaults to bundled figma-batch-enrich.md.",
)
@click.option(
    "--prompt", "prompt_text", default=None,
    help="Inline prompt template (overrides --prompt-file).",
)
@click.option(
    "--model", default="claude-sonnet-4-6",
    help="Claude model.",
)
@click.option(
    "--max-turns", type=int, default=50,
    help="Max turns (needs headroom for tool use + subagent round-trips).",
)
@click.option(
    "--glob", "glob_pattern", default="**/*.md",
    help="Glob for directory mode.",
)
@click.option("--changed-only", is_flag=True, help="Only process git-changed files.")
@click.option(
    "--needs-enrichment", is_flag=True,
    help="Filter to files needing enrichment (missing enriched_hash).",
)
@click.option(
    "--min-frames", type=int, default=0,
    help="Only process files with at least N frames (use with --needs-enrichment).",
)
@click.option(
    "--max-frames", type=int, default=0,
    help="Only process files with at most N frames (use with --needs-enrichment). 0 = no limit.",
)
@click.option(
    "--max-files", type=int, default=0,
    help="Limit to N files (0 = unlimited).",
)
@click.option("--dry-run", is_flag=True, help="Print file list without calling claude.")
@click.option(
    "--skip-permissions/--no-skip-permissions", default=True,
    help="Pass --dangerously-skip-permissions to claude (default: on for CI).",
)
@click.pass_context
def claude_run_cmd(
    ctx: click.Context,
    target: Path,
    prompt_file: Path | None,
    prompt_text: str | None,
    model: str,
    max_turns: int,
    glob_pattern: str,
    changed_only: bool,
    needs_enrichment: bool,
    min_frames: int,
    max_frames: int,
    max_files: int,
    dry_run: bool,
    skip_permissions: bool,
) -> None:
    """Launch claude -p for single-file or batch enrichment.

    TARGET is a file or directory to process. Each file is enriched
    individually with commit+push after each success.

    Outputs stream-json to stdout — pipe through ``figmaclaw stream-format``
    for human-readable CI logs.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not target.is_absolute():
        target = repo_dir / target

    # Resolve prompt template
    if prompt_text:
        template = prompt_text
    elif prompt_file:
        template = prompt_file.read_text()
    else:
        template = default_prompt_path().read_text()

    files = collect_files(
        target, glob_pattern, changed_only, needs_enrichment,
        min_frames=min_frames, max_frames=max_frames,
    )
    if max_files > 0 and len(files) > max_files:
        click.echo(f"[claude-run] limiting to {max_files}/{len(files)} files", err=True)
        files = files[:max_files]

    if not files:
        click.echo("[claude-run] No files found.", err=True)
        sys.exit(0)

    if dry_run:
        click.echo("[claude-run] DRY RUN — files that would be passed to claude:")
        for f in files:
            click.echo(f"  {f}")
        sys.exit(0)

    total = len(files)
    succeeded = 0
    failed = 0

    for i, file_path in enumerate(files, 1):
        click.echo(f"[claude-run] [{i}/{total}] enriching: {file_path}", err=True)
        prompt = build_prompt(template, file_path, [file_path])
        rc = _run_claude(
            prompt=prompt,
            model=model,
            max_turns=max_turns,
            skip_permissions=skip_permissions,
            extra_flags=[],
        )
        if rc != 0:
            click.echo(f"[claude-run] [{i}/{total}] FAILED (exit {rc}): {file_path}", err=True)
            failed += 1
        else:
            click.echo(f"[claude-run] [{i}/{total}] OK: {file_path}", err=True)
            succeeded += 1

    click.echo(f"[claude-run] Done: {succeeded} succeeded, {failed} failed out of {total}", err=True)
    sys.exit(1 if failed > 0 and succeeded == 0 else 0)
