"""figmaclaw mark-stale — force re-enrichment of a page.

Clears the enriched_* fields from frontmatter, causing the next
inspect --needs-enrichment check to report this page as needing work.

Use this when you know a page needs re-enrichment but the structural
hash hasn't changed (e.g., designer changed visual content without
changing frame structure).
"""

from __future__ import annotations

from pathlib import Path

import click
import yaml

from figmaclaw.figma_parse import parse_frontmatter, split_frontmatter
from figmaclaw.figma_render import _FlowDict, _FlowList, _FrontmatterDumper
from figmaclaw.git_utils import git_commit


@click.command("mark-stale")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def mark_stale_cmd(ctx: click.Context, md_path: Path, auto_commit: bool) -> None:
    """Force re-enrichment by clearing enrichment state from frontmatter.

    Removes enriched_hash, enriched_at, and enriched_frame_hashes, and ensures
    explicit enriched_schema_version=0 is present. Body is never touched.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    md_text = md_path.read_text()
    fm = parse_frontmatter(md_text)
    if fm is None:
        click.echo(f"error: {md_path}: no figmaclaw frontmatter found", err=True)
        ctx.exit(2)
        return

    parts = split_frontmatter(md_text)
    if parts is None:
        click.echo(f"error: {md_path}: failed to parse frontmatter", err=True)
        ctx.exit(2)
        return
    fm_block, body = parts

    # If already stale AND explicit schema version exists, nothing to do.
    if fm.enriched_hash is None and "enriched_schema_version:" in fm_block:
        click.echo(f"mark-stale: {md_path} is already not enriched — nothing to do")
        return

    # Rebuild frontmatter WITHOUT enriched_* fields, but preserve pull fields.
    fm_data: dict = {"file_key": fm.file_key, "page_node_id": fm.page_node_id}
    if fm.section_node_id:
        fm_data["section_node_id"] = fm.section_node_id
    if fm.frames:
        fm_data["frames"] = _FlowList(fm.frames)
    if fm.flows:
        fm_data["flows"] = _FlowList(fm.flows)

    # Always explicit: stale means legacy/unknown enriched output.
    fm_data["enriched_schema_version"] = 0

    if fm.component_set_keys:
        fm_data["component_set_keys"] = _FlowDict(fm.component_set_keys)
    if fm.raw_frames:
        fm_data["raw_frames"] = _FlowDict(
            {k: _FlowDict({"raw": v.raw, "ds": _FlowList(v.ds)}) for k, v in fm.raw_frames.items()}
        )
    if fm.raw_tokens:
        fm_data["raw_tokens"] = _FlowDict(
            {
                k: _FlowDict({"raw": v.raw, "stale": v.stale, "valid": v.valid})
                for k, v in fm.raw_tokens.items()
            }
        )
    if fm.frame_sections:
        fm_data["frame_sections"] = _FlowDict(
            {
                frame_id: _FlowList(
                    [
                        _FlowDict(
                            {
                                "node_id": n.node_id,
                                "name": n.name,
                                "x": n.x,
                                "y": n.y,
                                "w": n.w,
                                "h": n.h,
                                "instances": _FlowList(n.instances),
                                "instance_component_ids": _FlowList(n.instance_component_ids),
                                "raw_count": n.raw_count,
                            }
                        )
                        for n in nodes
                    ]
                )
                for frame_id, nodes in fm.frame_sections.items()
            }
        )

    new_fm_body = yaml.dump(
        fm_data,
        Dumper=_FrontmatterDumper,
        default_flow_style=False,
        allow_unicode=True,
        width=2**20,
    ).rstrip()

    md_path.write_text(f"---\n{new_fm_body}\n---\n{body}")

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    click.echo(f"mark-stale: cleared enrichment state from {rel}")

    if auto_commit and git_commit(repo_dir, [rel], f"sync: mark {rel} as stale"):
        click.echo(f"  committed: {rel}")
