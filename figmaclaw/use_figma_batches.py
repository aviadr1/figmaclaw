"""Shared use_figma batch-emission protocol and CLI options.

Both `apply-tokens` and `audit-page swap` slice their input rows into
batches, write a JSON rows file + a generated JS file per batch, and
manifest the result so an external runner can replay them. The protocol
shape is identical:

  <batch_dir>/
    <prefix>-NNNN.json            ← list of writer-shape row dicts
    <prefix>-NNNN.use_figma.js    ← generated Figma Plugin API JS
    manifest.json                 ← versioned descriptor

This module owns the protocol so both commands can't drift apart, and so
re-runs that produce fewer batches than the previous run reliably clean up
orphaned files (issue #167 review finding #3).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import click

from figmaclaw.figma_utils import write_json_if_changed


def clean_generated_batch_dir(batch_dir: Path, *, file_name_prefix: str) -> None:
    """Remove every previously-emitted batch file under *batch_dir*.

    Matches `<prefix>-NNNN.json`, `<prefix>-NNNN.use_figma.js`, and the
    sibling `manifest.json`. Anything else in the directory is left alone
    so the operator can keep their own artifacts (e.g. a hand-rolled
    `apply_colors_inline.js` referenced from the migration journal).
    """
    pattern = re.compile(rf"^{re.escape(file_name_prefix)}-\d{{4}}\.(json|use_figma\.js)$")
    for path in batch_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == "manifest.json" or pattern.match(path.name):
            path.unlink()


def write_use_figma_batches[Row](
    rows: Sequence[Row],
    *,
    batch_dir: Path,
    batch_size: int,
    file_name_prefix: str,
    file_key: str,
    row_to_dict: Callable[[Row], dict[str, Any]],
    render_js: Callable[[list[dict[str, Any]]], str],
    description_prefix: str,
    manifest_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Slice *rows* into batches, write artifacts, return manifest + calls.

    Parameters
    ----------
    rows :
        The accepted-and-validated input. The caller is responsible for
        having already filtered out refusals.
    batch_dir :
        Output directory; created if missing. Stale prior-run files matching
        the prefix are deleted before writing.
    batch_size :
        Maximum number of rows per emitted batch. Must be > 0.
    file_name_prefix :
        Filename stem before the `-NNNN` index — e.g. ``"batch"`` for
        apply-tokens, ``"swap-batch"`` for audit-page swap.
    file_key :
        Figma file key, embedded in the runtime-call descriptors so the
        external executor (`use_figma_exec`) can target the right file.
    row_to_dict :
        Maps a typed row into the JSON-serialisable writer-shape dict that
        ends up in the per-batch JSON file (and is fed to `render_js`).
    render_js :
        Renders the per-batch JS file given the list of writer-shape dicts.
    description_prefix :
        Human-readable prefix for the per-batch description ("apply design
        token bindings batch", "audit-page swap batch", …); the index
        is appended automatically.
    manifest_extras :
        Additional keys merged into the emitted ``manifest.json`` — used to
        carry per-command metadata (``namespace``, ``node_map``, ``kind``,
        the schema version, etc.) without bloating this signature.

    Returns
    -------
    ``{"manifest": <dict>, "calls": <list of {file_key, code, description}>}``.
    The manifest matches what gets written to ``manifest.json``; the calls
    list is what `execute_use_figma_calls` consumes.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    batch_dir.mkdir(parents=True, exist_ok=True)
    clean_generated_batch_dir(batch_dir, file_name_prefix=file_name_prefix)

    batches: list[dict[str, Any]] = []
    calls: list[dict[str, str]] = []

    for batch_index, start in enumerate(range(0, len(rows), batch_size), start=1):
        batch_rows = list(rows[start : start + batch_size])
        writer_rows = [row_to_dict(row) for row in batch_rows]
        rows_path = batch_dir / f"{file_name_prefix}-{batch_index:04d}.json"
        js_path = batch_dir / f"{file_name_prefix}-{batch_index:04d}.use_figma.js"
        write_json_if_changed(rows_path, writer_rows)
        js = render_js(writer_rows)
        js_path.write_bytes(js.encode("utf-8"))
        description = f"{description_prefix} {batch_index:04d}"
        batches.append(
            {
                "index": batch_index,
                "rows": len(batch_rows),
                "rows_path": rows_path.name,
                "js_path": js_path.name,
                "description": description,
            }
        )
        calls.append({"file_key": file_key, "code": js, "description": description})

    manifest: dict[str, Any] = {
        "batch_size": batch_size,
        "total_rows": len(rows),
        "batch_count": len(batches),
        "batches": batches,
    }
    if manifest_extras:
        manifest = {**manifest_extras, **manifest}
    write_json_if_changed(batch_dir / "manifest.json", manifest)
    return {"manifest": manifest, "calls": calls}


def use_figma_batch_options(default_batch_size: int) -> Callable[[Callable], Callable]:
    """Stack the shared dry-run / emit-only / execute click option set.

    Both `apply-tokens` and `audit-page swap` expose the same protocol:

      --dry-run / --emit-only / --execute   (last-wins; share the `mode` dest)
      --resume-from N             (1-based, only meaningful in --execute)
      --continue-on-error         (keep going past a failed batch)
      --batch-size N              (per-command default)
      --batch-dir PATH            (mandatory for emit-only / execute)
      --json                      (structured output)

    The three mode flags share Click's ``mode`` dest via ``flag_value``, so
    passing more than one is not an error — the last one on the command line
    wins. (Click does not enforce mutual exclusion across ``flag_value``
    options, and a callback-based mutex would surprise operators who
    interpret ``--dry-run --execute`` as "I changed my mind" — which is the
    same semantics they get from ``--dry-run`` later in the same line.)
    Centralising the decorator ensures the two commands cannot drift out of
    sync — once we add (say) ``--retry-budget`` here it lights up for both.
    """

    def decorator(func: Callable) -> Callable:
        # click stacks decorators bottom-up, so list them in REVERSE of how
        # they should appear in --help.
        func = click.option("--json", "json_output", is_flag=True, help="Output structured JSON.")(
            func
        )
        func = click.option(
            "--continue-on-error",
            is_flag=True,
            help="Keep executing after a failed batch.",
        )(func)
        func = click.option(
            "--resume-from",
            type=int,
            default=1,
            show_default=True,
            help="1-based batch number to start from in --execute mode.",
        )(func)
        func = click.option(
            "--execute",
            "mode",
            flag_value="execute",
            help="Write batches and run them through the use_figma executor.",
        )(func)
        func = click.option(
            "--emit-only",
            "mode",
            flag_value="emit-only",
            help="Write deterministic batches but do not run them.",
        )(func)
        func = click.option(
            "--dry-run",
            "mode",
            flag_value="dry-run",
            default="dry-run",
            help="Plan only — do not write or execute anything.",
        )(func)
        func = click.option(
            "--batch-size",
            type=int,
            default=default_batch_size,
            show_default=True,
        )(func)
        func = click.option(
            "--batch-dir",
            "batch_dir",
            type=click.Path(file_okay=False, path_type=Path),
            help="Directory for emitted batch JSON, JS, and manifest files.",
        )(func)
        return func

    return decorator


__all__ = [
    "clean_generated_batch_dir",
    "use_figma_batch_options",
    "write_use_figma_batches",
]
