#!/usr/bin/env python3
"""
claude_run.py — Thin launcher for claude -p file enrichment tasks.

Single file  → one claude -p with Read/Bash/Figma tools, multi-turn
Directory    → one claude -p (orchestrator) that spawns parallel subagents via Agent tool;
               claude decides batching/parallelism, not Python

Prompt template placeholders:
  {file_path}     single-file path
  {file_content}  single-file content
  {filename}      bare filename
  {file_list}     newline-separated list of paths  (directory mode)
  {target_dir}    directory being processed         (directory mode)

Usage:
    # Single file
    python -m figmaclaw.scripts.claude_run file.md --prompt-file prompts/enrich.md

    # Whole directory (claude spawns parallel subagents internally)
    python -m figmaclaw.scripts.claude_run linear/ --prompt-file prompts/batch-enrich.md

    # Only git-changed files (CI hook)
    python -m figmaclaw.scripts.claude_run linear/ --prompt-file prompts/batch-enrich.md --changed-only

    # Dry run — show which files would be passed to claude
    python -m figmaclaw.scripts.claude_run linear/ --prompt-file prompts/batch-enrich.md --dry-run

Environment:
    CLAUDE_CODE_OAUTH_TOKEN — auth for claude (auto-loaded from .env if python-dotenv installed)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _log(msg: str) -> None:
    print(f"[claude_run] {msg}", file=sys.stderr, flush=True)


def _changed_files(base: Path, glob_pattern: str) -> list[Path]:
    """Git-modified + untracked files under base matching glob."""
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


MAX_FRAMES_PER_FILE = 80  # skip files with more frames than this — they timeout CI


def _enrichment_info(md_path: Path) -> tuple[bool, int]:
    """Fast check: does file need enrichment? Returns (needs_it, frame_count).

    Reads the file directly — no subprocess. Checks:
    - Has enriched_hash in frontmatter? If yes → skip (already enriched)
    - Count body table rows for frame size estimate
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


def _collect_files(
    target: Path,
    glob_pattern: str,
    changed_only: bool,
    needs_enrichment: bool = False,
) -> list[Path]:
    if target.is_file():
        return [target]
    if changed_only:
        files = _changed_files(target, glob_pattern)
    else:
        files = sorted(target.glob(glob_pattern))
    if needs_enrichment:
        before = len(files)
        enrichable: list[tuple[Path, int]] = []
        skipped_big = 0
        for f in files:
            needs_it, frame_count = _enrichment_info(f)
            if not needs_it:
                continue
            if frame_count > MAX_FRAMES_PER_FILE:
                skipped_big += 1
                _log(f"skip {f} ({frame_count} frames > {MAX_FRAMES_PER_FILE} max)")
                continue
            enrichable.append((f, frame_count))
        # Sort smallest first — enrich many small files before hitting big ones
        enrichable.sort(key=lambda x: x[1])
        files = [f for f, _ in enrichable]
        msg = f"{len(files)}/{before} files need enrichment"
        if skipped_big:
            msg += f" ({skipped_big} skipped: >{MAX_FRAMES_PER_FILE} frames)"
        _log(msg)
    else:
        _log(f"{len(files)} files to process")
    return files


def _build_prompt(template: str, target: Path, files: list[Path]) -> str:
    """Build prompt for a single file. Always fills {file_path}."""
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


def _run_claude(
    prompt: str,
    model: str,
    max_turns: int,
    skip_permissions: bool,
    extra_flags: list[str],
) -> int:
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
        stdout=subprocess.PIPE,  # stream-json → our stdout (pipeable / tee-able)
        stderr=subprocess.PIPE,  # claude's own stderr → our stderr
    )
    assert proc.stdin and proc.stdout and proc.stderr

    proc.stdin.write(prompt.encode())
    proc.stdin.close()

    import threading

    def _relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()

    t = threading.Thread(target=_relay_stderr, daemon=True)
    t.start()

    # Forward stream-json to stdout so callers can pipe/tee it
    for line in proc.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    t.join()
    return proc.wait()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch claude -p for single-file or batch enrichment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="File or directory to process")

    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt-file", metavar="PATH", help="Prompt template file")
    prompt_group.add_argument("--prompt", metavar="TEXT", help="Inline prompt template")

    parser.add_argument(
        "--model", default="claude-sonnet-4-6",
        help="Claude model (default: claude-sonnet-4-6; use haiku for bulk cheapness)",
    )
    parser.add_argument(
        "--max-turns", type=int, default=50,
        help="Max turns (default: 50; orchestrator + subagent rounds need headroom)",
    )
    parser.add_argument(
        "--glob", default="**/*.md", metavar="PATTERN",
        help="Glob for directory mode (default: **/*.md)",
    )
    parser.add_argument(
        "--changed-only", action="store_true",
        help="Only pass git-changed files to claude (for CI hooks)",
    )
    parser.add_argument(
        "--needs-enrichment", action="store_true",
        help="Filter to files with missing descriptions (uses enriched_hash frontmatter check)",
    )
    parser.add_argument(
        "--max-files", type=int, default=0, metavar="N",
        help="Limit to N files (0 = unlimited). Use with --needs-enrichment to batch across CI runs.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print resolved file list without calling claude",
    )
    parser.add_argument(
        "--skip-permissions", action="store_true", default=True,
        help="Pass --dangerously-skip-permissions (default: on for CI/CD use)",
    )
    parser.add_argument(
        "--no-skip-permissions", dest="skip_permissions", action="store_false",
        help="Require interactive tool approval (local dev safety)",
    )
    parser.add_argument(
        "--extra-flags", nargs=argparse.REMAINDER, default=[],
        help="Extra flags forwarded verbatim to claude -p",
    )

    args = parser.parse_args()
    target = Path(args.target)

    template = (
        Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    )

    files = _collect_files(target, args.glob, args.changed_only, args.needs_enrichment)
    if args.max_files > 0 and len(files) > args.max_files:
        _log(f"limiting to {args.max_files}/{len(files)} files")
        files = files[:args.max_files]

    if not files:
        _log("No files found.")
        sys.exit(0)

    if args.dry_run:
        print("[claude_run] DRY RUN — files that would be passed to claude:")
        for f in files:
            print(f"  {f}")
        sys.exit(0)

    # Process each file individually — commit+push after each success
    total = len(files)
    succeeded = 0
    failed = 0

    for i, file_path in enumerate(files, 1):
        _log(f"[{i}/{total}] enriching: {file_path}")
        prompt = _build_prompt(template, file_path, [file_path])
        rc = _run_claude(
            prompt=prompt,
            model=args.model,
            max_turns=args.max_turns,
            skip_permissions=args.skip_permissions,
            extra_flags=args.extra_flags,
        )
        if rc != 0:
            _log(f"[{i}/{total}] FAILED (exit {rc}): {file_path}")
            failed += 1
        else:
            _log(f"[{i}/{total}] OK: {file_path}")
            succeeded += 1

    _log(f"Done: {succeeded} succeeded, {failed} failed out of {total}")
    sys.exit(1 if failed > 0 and succeeded == 0 else 0)


if __name__ == "__main__":
    main()
