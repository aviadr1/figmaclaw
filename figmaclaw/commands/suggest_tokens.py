"""figmaclaw suggest-tokens — match raw token values to DS variable candidates."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import click

from figmaclaw.commands._shared import load_state
from figmaclaw.token_catalog import catalog_staleness_errors, load_catalog, suggest_for_sidecar


def _default_output_path(sidecar_file: Path) -> Path:
    """Sibling output path: ``foo.tokens.json`` → ``foo.suggestions.json``.

    For sidecars that don't follow the ``.tokens.json`` convention,
    fall back to replacing the final ``.json`` with ``.suggestions.json``.
    """
    name = sidecar_file.name
    if name.endswith(".tokens.json"):
        base = name[: -len(".tokens.json")]
        return sidecar_file.parent / f"{base}.suggestions.json"
    if name.endswith(".json"):
        base = name[: -len(".json")]
        return sidecar_file.parent / f"{base}.suggestions.json"
    return sidecar_file.parent / f"{name}.suggestions.json"


@click.command("suggest-tokens")
@click.option(
    "--sidecar",
    "sidecar_path",
    required=True,
    type=click.Path(exists=True),
    help="Path to the .tokens.json sidecar file (read-only — never mutated).",
)
@click.option(
    "--output",
    "-o",
    "output_path_arg",
    default=None,
    help=(
        "Where to write the suggestions JSON. "
        "Default: sibling of the sidecar with .suggestions.json suffix. "
        "Use '-' for stdout."
    ),
)
@click.option(
    "--frame",
    "frames",
    multiple=True,
    help="Only process frames matching this name substring (case-insensitive). "
    "May be specified multiple times.",
)
@click.option(
    "--library",
    "libraries",
    multiple=True,
    help=(
        "Only suggest tokens from libraries whose name OR library_hash contains "
        "this substring (case-insensitive). May be specified multiple times. "
        "Essential for migration audits — without it, suggestions can point at "
        "the OLD design system instead of the migration target. "
        "Example: --library tap --library lsn"
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print summary but write nothing (sidecar, output file, or stdout).",
)
@click.pass_context
def suggest_tokens_cmd(
    ctx: click.Context,
    sidecar_path: str,
    output_path_arg: str | None,
    frames: tuple[str, ...],
    libraries: tuple[str, ...],
    dry_run: bool,
) -> None:
    """Match raw token values to DS variable candidates.

    Loads the DS variable catalog built during pull and produces a
    suggestions JSON annotating each issue with ``suggest_status`` and
    candidate variable IDs. The input sidecar is never mutated; output
    goes to a separate file (default sibling) so CI re-pulls and webhook
    refreshes don't silently revert audit annotations.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    catalog = load_catalog(repo_dir)

    # When the JSON output is going to stdout, redirect informational
    # messages to stderr so consumers can pipe to `jq` / similar.
    info_to_stderr = output_path_arg == "-"

    def info(msg: str = "") -> None:
        click.echo(msg, err=info_to_stderr)

    sidecar_file = Path(sidecar_path)
    sidecar = json.loads(sidecar_file.read_text(encoding="utf-8"))
    file_key = sidecar.get("file_key")
    if file_key:
        errors = catalog_staleness_errors(catalog, load_state(repo_dir), file_key)
        if errors:
            raise click.ClickException(errors[0])

    # Apply frame filter if requested. The output reflects exactly what
    # was processed — filtered runs produce filtered output files.
    if frames:
        filtered_frames = {
            fid: fdata
            for fid, fdata in sidecar.get("frames", {}).items()
            if any(f.lower() in fdata.get("name", "").lower() for f in frames)
        }
        total_frames = len(sidecar.get("frames", {}))
        work_sidecar = dict(sidecar)
        work_sidecar["frames"] = filtered_frames
    else:
        filtered_frames = sidecar.get("frames", {})
        total_frames = len(filtered_frames)
        work_sidecar = sidecar

    frames_processed = len(filtered_frames)

    # Count catalog stats — use the schema-v2 values_by_mode shape.
    # (Pre-v2 catalogs are migrated on load; here we only see v2 entries.)
    def _has_color(v: object) -> bool:
        for value in getattr(v, "values_by_mode", {}).values():
            if getattr(value, "hex", None):
                return True
        return False

    def _has_numeric(v: object) -> bool:
        for value in getattr(v, "values_by_mode", {}).values():
            if getattr(value, "numeric_value", None) is not None:
                return True
        return False

    color_vars = sum(1 for v in catalog.variables.values() if _has_color(v))
    numeric_vars = sum(1 for v in catalog.variables.values() if _has_numeric(v))
    total_vars = len(catalog.variables)

    info(f"Catalog: {total_vars} variables ({color_vars} color, {numeric_vars} numeric)")
    info(f"Frames processed: {frames_processed} / {total_frames}")

    library_hash_filter: set[str] | None = None
    if libraries:
        # Resolve each substring against library NAME or library_hash KEY.
        matched: dict[str, str] = {}  # hash -> name (for display)
        for needle in libraries:
            n = needle.lower()
            for lib_hash, lib in catalog.libraries.items():
                if n in lib_hash.lower() or n in (lib.name or "").lower():
                    matched[lib_hash] = lib.name or lib_hash
        if not matched:
            raise click.ClickException(
                f"--library filter matched no libraries in the catalog. "
                f"Searched for: {', '.join(libraries)!r}. "
                f"Run `figmaclaw doctor` or inspect .figma-sync/ds_catalog.json "
                f"to see available libraries."
            )
        library_hash_filter = set(matched.keys())
        # Count how many variables that filter selects, so the user can
        # spot a near-miss substring that picked the wrong library.
        kept_vars = sum(
            1 for v in catalog.variables.values() if v.library_hash in library_hash_filter
        )
        info(f"Library filter: {len(matched)} libraries, {kept_vars} candidate variables")
        for lib_hash, name in sorted(matched.items(), key=lambda x: x[1].lower()):
            n_vars = sum(1 for v in catalog.variables.values() if v.library_hash == lib_hash)
            info(f"  ✓ {name}  ({lib_hash}, {n_vars} vars)")

    suggest_for_sidecar(work_sidecar, catalog, library_hashes=library_hash_filter)

    # Collect results stats — use count field (schema v2) or default to 1 (v1)
    auto = ambiguous = no_match = 0
    prop_value_no_match: Counter[tuple[str, str]] = Counter()

    for fdata in work_sidecar.get("frames", {}).values():
        for issue in fdata.get("issues", []):
            count = issue.get("count", 1)
            status = issue.get("suggest_status", "no_match")
            if status == "auto":
                auto += count
            elif status == "ambiguous":
                ambiguous += count
            else:
                no_match += count
                prop = issue.get("property", "unknown")
                val = issue.get("current_value")
                if isinstance(val, float):
                    val_str = f"{val:.3f}".rstrip("0").rstrip(".")
                elif isinstance(val, int):
                    val_str = str(val)
                elif isinstance(val, dict):
                    val_str = issue.get("hex", str(val))
                else:
                    val_str = str(val)
                prop_value_no_match[(prop, val_str)] += count

    info("")
    info("Results:")
    info(f"  auto:      {auto:>5}  (will be set automatically)")
    info(f"  ambiguous: {ambiguous:>5}  (need manual review — multiple token candidates)")
    info(f"  no_match:  {no_match:>5}  (no DS variable found for this value)")

    if prop_value_no_match:
        info("")
        info("Top no_match values:")
        for (prop, val_str), count in prop_value_no_match.most_common(10):
            info(f"  {prop} {val_str}: {count} occurrences")

    if dry_run:
        return

    output_json = json.dumps(work_sidecar, indent=2, ensure_ascii=False)

    if output_path_arg == "-":
        sys.stdout.write(output_json)
        if not output_json.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return

    output_file = Path(output_path_arg) if output_path_arg else _default_output_path(sidecar_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(output_json + "\n", encoding="utf-8")
    info(f"\nWrote suggestions → {output_file}")
