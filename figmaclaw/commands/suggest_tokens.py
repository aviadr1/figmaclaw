"""figmaclaw suggest-tokens — match raw token values to DS variable candidates."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import click

from figmaclaw.token_catalog import load_catalog, suggest_for_sidecar


@click.command("suggest-tokens")
@click.option(
    "--sidecar",
    "sidecar_path",
    required=True,
    type=click.Path(exists=True),
    help="Path to the .tokens.json sidecar file.",
)
@click.option(
    "--frame",
    "frames",
    multiple=True,
    help="Only process frames matching this name substring (case-insensitive). "
    "May be specified multiple times.",
)
@click.option("--dry-run", is_flag=True, help="Print summary but don't write changes.")
@click.pass_context
def suggest_tokens_cmd(
    ctx: click.Context,
    sidecar_path: str,
    frames: tuple[str, ...],
    dry_run: bool,
) -> None:
    """Match raw token values to DS variable candidates.

    Loads the DS variable catalog built during pull and annotates each issue in
    the sidecar with suggest_status and fix_variable_id (when unambiguous).
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    catalog = load_catalog(repo_dir)

    sidecar_file = Path(sidecar_path)
    sidecar = json.loads(sidecar_file.read_text(encoding="utf-8"))

    # Apply frame filter if requested
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

    click.echo(f"Catalog: {total_vars} variables ({color_vars} color, {numeric_vars} numeric)")
    click.echo(f"Frames processed: {frames_processed} / {total_frames}")

    suggest_for_sidecar(work_sidecar, catalog)

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

    click.echo("")
    click.echo("Results:")
    click.echo(f"  auto:      {auto:>5}  (will be set automatically)")
    click.echo(f"  ambiguous: {ambiguous:>5}  (need manual review — multiple token candidates)")
    click.echo(f"  no_match:  {no_match:>5}  (no DS variable found for this value)")

    if prop_value_no_match:
        click.echo("")
        click.echo("Top no_match values:")
        for (prop, val_str), count in prop_value_no_match.most_common(10):
            click.echo(f"  {prop} {val_str}: {count} occurrences")

    if not dry_run:
        # If we only processed a subset of frames, merge back into original sidecar
        if frames:
            for fid, fdata in filtered_frames.items():
                sidecar["frames"][fid] = fdata
            sidecar["suggested_at"] = work_sidecar.get("suggested_at", "")
        else:
            sidecar = work_sidecar
        sidecar_file.write_text(
            json.dumps(sidecar, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        click.echo(f"\nWrote updated sidecar → {sidecar_path}")
