"""figmaclaw claude-run — launch Claude Code for file enrichment in CI.

Thin orchestrator: discovers files, filters by enrichment status, then
invokes ``claude -p`` for each file with a prompt template.

Modes:
  - **Whole-page** (default): one claude -p per file with the batch-enrich prompt.
  - **Section-mode** (``--section-mode``): for large pages (>SECTION_THRESHOLD frames),
    enriches one section at a time. Each section gets its own Claude invocation and
    commit. After all sections are done, a finalization step writes the page summary,
    Screen flows mermaid, and calls mark-enriched.

Prompt template placeholders:
  {file_path}       single-file path
  {file_content}    single-file content
  {filename}        bare filename
  {file_list}       newline-separated list of paths  (directory mode)
  {target_dir}      directory being processed         (directory mode)
  {section_node_id} section node ID (section-mode only)
  {section_name}    section name (section-mode only)
"""

from __future__ import annotations

import importlib.resources
import subprocess
import sys
import threading
import time
from pathlib import Path

import click

from figmaclaw.figma_md_parse import section_line_ranges

SECTION_THRESHOLD = 80  # pages/sections above this use incremental mode
ENRICHMENT_LOG = ".figma-sync/enrichment-log.csv"


def _log_enrichment(
    repo_dir: Path, file_path: Path, mode: str,
    frames: int, duration_s: float, success: bool,
    section_name: str = "",
) -> None:
    """Append one row to the enrichment log for empirical analysis."""
    log_path = repo_dir / ENRICHMENT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text("timestamp,file,mode,frames,duration_s,success,section\n")
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    rel = str(file_path.relative_to(repo_dir)) if file_path.is_relative_to(repo_dir) else str(file_path)
    row = f"{ts},{rel},{mode},{frames},{duration_s:.0f},{success},{section_name}\n"
    with open(log_path, "a") as f:
        f.write(row)


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

    # Must have figmaclaw frontmatter to be enrichable
    if "file_key:" not in text:
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


# ---------------------------------------------------------------------------
# Section-level enrichment helpers
# ---------------------------------------------------------------------------


def pending_sections(md_path: Path) -> list[dict[str, str | int]]:
    """Return sections that need enrichment (have pending placeholders).

    Returns ``[{"node_id": ..., "name": ..., "pending_frames": N}]`` for
    sections with ``(no description yet)`` placeholders.
    """
    try:
        text = md_path.read_text()
    except OSError:
        return []

    lines = text.splitlines()
    result: list[dict[str, str | int]] = []
    for section, start, end in section_line_ranges(text):
        if not section.node_id:
            continue  # skip Screen flows etc.
        pending = sum(
            1 for line in lines[start:end]
            if "| (no description yet) |" in line
        )
        if pending > 0:
            result.append({
                "node_id": section.node_id,
                "name": section.name,
                "pending_frames": pending,
            })
    return result


def needs_finalization(md_path: Path) -> bool:
    """True when all sections are described but the page isn't marked enriched yet.

    This means section-by-section enrichment is complete and the finalization
    step (page summary + mermaid + mark-enriched) should run.
    """
    try:
        text = md_path.read_text()
    except OSError:
        return False

    # If there are still pending placeholders, not ready
    if "| (no description yet) |" in text:
        return False

    # If already marked as enriched, no need to finalize
    fm_block = text.split("\n---")[0] if "\n---" in text else ""
    if "enriched_hash:" in fm_block:
        return False

    return True


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def build_prompt(
    template: str,
    target: Path,
    files: list[Path],
    section_node_id: str = "",
    section_name: str = "",
    section_list: str = "",
) -> str:
    """Fill template placeholders for a single file, section, or batch."""
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
        .replace("{section_node_id}", section_node_id)
        .replace("{section_name}", section_name)
        .replace("{section_list}", section_list)
    )


def _prompt_path(name: str) -> Path:
    """Return path to a bundled prompt template."""
    return Path(str(importlib.resources.files("figmaclaw.prompts").joinpath(name)))


def default_prompt_path() -> Path:
    """Return the path to the bundled ``figma-batch-enrich.md`` prompt."""
    return _prompt_path("figma-batch-enrich.md")


def frames_prompt_path() -> Path:
    """Return the path to the bundled ``figma-frames-enrich.md`` prompt."""
    return _prompt_path("figma-frames-enrich.md")


def section_prompt_path() -> Path:
    """Return the path to the bundled ``figma-section-enrich.md`` prompt."""
    return _prompt_path("figma-section-enrich.md")


def finalize_prompt_path() -> Path:
    """Return the path to the bundled ``figma-section-finalize.md`` prompt."""
    return _prompt_path("figma-section-finalize.md")


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
@click.option(
    "--section-mode", is_flag=True,
    help="For large pages (>80 frames), enrich one section at a time.",
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
    section_mode: bool,
    dry_run: bool,
    skip_permissions: bool,
) -> None:
    """Launch claude -p for single-file or batch enrichment.

    TARGET is a file or directory to process. Each file is enriched
    individually with commit+push after each success.

    With --section-mode, large pages (>80 frames) are enriched one section at
    a time. Each section gets its own Claude invocation and commit. After all
    sections are done, a finalization step writes the page summary + mermaid
    and calls mark-enriched.

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
            _, fc = enrichment_info(f)
            if section_mode and fc > SECTION_THRESHOLD:
                sections = pending_sections(f)
                fin = needs_finalization(f)
                click.echo(f"  {f} ({fc} frames, section-mode: {len(sections)} pending sections, finalize={fin})")
                for s in sections:
                    click.echo(f"    section {s['node_id']} ({s['name']}): {s['pending_frames']} pending")
            else:
                click.echo(f"  {f} ({fc} frames)")
        sys.exit(0)

    total = len(files)
    succeeded = 0
    failed = 0

    for i, file_path in enumerate(files, 1):
        # Pull latest to avoid re-enriching files another run already handled.
        # Each Claude invocation pushes after commit, so concurrent/sequential
        # runs may have enriched files since our initial checkout.
        subprocess.run(["git", "pull", "--no-rebase"], capture_output=True)

        # Re-check after pull — file may now have enriched_hash
        needs_it, frame_count = enrichment_info(file_path)
        if not needs_it:
            click.echo(f"[claude-run] [{i}/{total}] skip (already enriched): {file_path}", err=True)
            continue

        if section_mode and frame_count > SECTION_THRESHOLD:
            # Batch mode: describe up to 80 pending frames per Claude invocation
            # using write-descriptions (cross-section, mechanical row updates).
            # Much faster than per-section invocations — eliminates startup overhead.
            batch_template = _prompt_path("figma-sections-batch.md").read_text()
            sections = pending_sections(file_path)
            total_pending = sum(int(s["pending_frames"]) for s in sections)
            click.echo(
                f"[claude-run] [{i}/{total}] batch-mode: {file_path} "
                f"({total_pending} pending frames across {len(sections)} sections)",
                err=True,
            )

            chunk_num = 0
            prev_pending_count = None
            stale_retries = 0
            while sections:
                chunk_num += 1
                total_pending = sum(int(s["pending_frames"]) for s in sections)

                # Detect stuck loop: if pending count hasn't decreased after
                # a successful batch, the remaining frames are undescribable
                # (e.g. screenshot download fails). Skip to next file after 2 retries.
                if prev_pending_count is not None and total_pending >= prev_pending_count:
                    stale_retries += 1
                    if stale_retries >= 2:
                        click.echo(
                            f"[claude-run] [{i}/{total}] STUCK: {total_pending} frames "
                            f"won't describe (likely unrenderable screenshots). "
                            f"Moving to next file.",
                            err=True,
                        )
                        _log_enrichment(repo_dir, file_path, "stuck", total_pending, 0, False)
                        break
                else:
                    stale_retries = 0
                prev_pending_count = total_pending

                section_names = ", ".join(
                    str(s["name"]) for s in sections[:5]
                ) + (f" +{len(sections)-5} more" if len(sections) > 5 else "")

                click.echo(
                    f"[claude-run] [{i}/{total}] batch {chunk_num} "
                    f"({total_pending} pending): {section_names}",
                    err=True,
                )
                t0 = time.monotonic()
                prompt = build_prompt(
                    batch_template, file_path, [file_path],
                    section_list=section_names,
                )
                rc = _run_claude(
                    prompt=prompt, model=model, max_turns=max_turns,
                    skip_permissions=skip_permissions, extra_flags=[],
                )
                dur = time.monotonic() - t0
                ok = rc == 0
                _log_enrichment(repo_dir, file_path, "batch", total_pending, dur, ok)
                if not ok:
                    click.echo(
                        f"[claude-run] [{i}/{total}] batch FAILED (exit {rc}, {dur:.0f}s): "
                        f"{file_path}",
                        err=True,
                    )
                    failed += 1
                    break
                click.echo(
                    f"[claude-run] [{i}/{total}] batch OK ({dur:.0f}s): {file_path}",
                    err=True,
                )
                succeeded += 1

                # Re-check pending after commit
                subprocess.run(["git", "pull", "--no-rebase"], capture_output=True)
                sections = pending_sections(file_path)

            # All frames described → finalize (page summary + section intros + mermaid)
            if needs_finalization(file_path):
                click.echo(
                    f"[claude-run] [{i}/{total}] finalizing: {file_path}",
                    err=True,
                )
                t0 = time.monotonic()
                fin_template = finalize_prompt_path().read_text()
                prompt = build_prompt(fin_template, file_path, [file_path])
                rc = _run_claude(
                    prompt=prompt, model=model, max_turns=max_turns,
                    skip_permissions=skip_permissions, extra_flags=[],
                )
                dur = time.monotonic() - t0
                ok = rc == 0
                _log_enrichment(repo_dir, file_path, "finalize", frame_count, dur, ok)
                if not ok:
                    click.echo(
                        f"[claude-run] [{i}/{total}] finalize FAILED (exit {rc}, {dur:.0f}s): "
                        f"{file_path}",
                        err=True,
                    )
                    failed += 1
                else:
                    click.echo(
                        f"[claude-run] [{i}/{total}] finalize OK ({dur:.0f}s): {file_path}",
                        err=True,
                    )
                    succeeded += 1
        else:
            # Standard whole-page enrichment
            click.echo(f"[claude-run] [{i}/{total}] enriching: {file_path} ({frame_count} frames)", err=True)
            t0 = time.monotonic()
            prompt = build_prompt(template, file_path, [file_path])
            rc = _run_claude(
                prompt=prompt, model=model, max_turns=max_turns,
                skip_permissions=skip_permissions, extra_flags=[],
            )
            dur = time.monotonic() - t0
            ok = rc == 0
            _log_enrichment(repo_dir, file_path, "whole-page", frame_count, dur, ok)
            if not ok:
                click.echo(f"[claude-run] [{i}/{total}] FAILED (exit {rc}, {dur:.0f}s): {file_path}", err=True)
                failed += 1
            else:
                click.echo(f"[claude-run] [{i}/{total}] OK ({dur:.0f}s, {frame_count} frames): {file_path}", err=True)
                succeeded += 1

    click.echo(f"[claude-run] Done: {succeeded} succeeded, {failed} failed out of {total}", err=True)
    sys.exit(1 if failed > 0 and succeeded == 0 else 0)
