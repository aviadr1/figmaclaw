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
from figmaclaw.figma_render import _FlowList, _FrontmatterDumper
from figmaclaw.git_utils import git_commit


@click.command("mark-stale")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def mark_stale_cmd(ctx: click.Context, md_path: Path, auto_commit: bool) -> None:
    """Force re-enrichment by clearing enrichment state from frontmatter.

    Removes enriched_hash, enriched_at, and enriched_frame_hashes.
    Body is never touched.
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

    if fm.enriched_hash is None:
        click.echo(f"mark-stale: {md_path} is already not enriched — nothing to do")
        return

    parts = split_frontmatter(md_text)
    if parts is None:
        click.echo(f"error: {md_path}: failed to parse frontmatter", err=True)
        ctx.exit(2)
        return
    _, body = parts

    # Rebuild frontmatter WITHOUT enriched_* fields
    fm_data: dict = {"file_key": fm.file_key, "page_node_id": fm.page_node_id}
    if fm.section_node_id:
        fm_data["section_node_id"] = fm.section_node_id
    if fm.frames:
        fm_data["frames"] = _FlowList(fm.frames)
    if fm.flows:
        fm_data["flows"] = _FlowList(fm.flows)
    # enriched_* intentionally omitted

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
