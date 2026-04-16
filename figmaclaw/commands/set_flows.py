"""figmaclaw set-flows — write flows into a figmaclaw .md file's frontmatter.

Performs a surgical in-place update of the `flows:` field in the YAML
frontmatter. Body prose is NEVER touched — the LLM owns it.

No Figma API call is made.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import yaml

from figmaclaw.figma_parse import parse_frontmatter, split_frontmatter
from figmaclaw.figma_render import _FlowDict, _FlowList, _FrontmatterDumper
from figmaclaw.git_utils import git_commit


def _apply_flows(md: str, flows: list[list[str]]) -> str:
    """Replace the flows list in the YAML frontmatter block. Preserves everything else."""
    fm = parse_frontmatter(md)
    if fm is None:
        raise click.UsageError("No figmaclaw frontmatter found — is this a figmaclaw .md file?")

    fm_data: dict = {"file_key": fm.file_key, "page_node_id": fm.page_node_id}
    if fm.section_node_id:
        fm_data["section_node_id"] = fm.section_node_id
    if fm.frames:
        fm_data["frames"] = _FlowList(fm.frames)
    if flows:
        fm_data["flows"] = _FlowList(flows)

    # Preserve enrichment state and explicit schema version.
    if fm.enriched_hash is not None:
        fm_data["enriched_hash"] = fm.enriched_hash
    if fm.enriched_at is not None:
        fm_data["enriched_at"] = fm.enriched_at
    if fm.enriched_frame_hashes:
        fm_data["enriched_frame_hashes"] = _FlowDict(fm.enriched_frame_hashes)
    fm_data["enriched_schema_version"] = fm.enriched_schema_version

    # Preserve pull-pass fields.
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
    parts = split_frontmatter(md)
    if parts is None:
        raise click.UsageError("No figmaclaw frontmatter found — is this a figmaclaw .md file?")
    _, body = parts
    return f"---\n{new_fm_body}\n---\n{body}"


@click.command("set-flows")
@click.argument("md_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--flows",
    "flows_input",
    required=True,
    help='JSON array [["src", "dst"], ...] to set as the flows list.',
)
@click.option("--auto-commit", "auto_commit", is_flag=True, help="git commit the result.")
@click.pass_context
def set_flows_cmd(
    ctx: click.Context,
    md_path: Path,
    flows_input: str,
    auto_commit: bool,
) -> None:
    """Write flows into a figmaclaw .md file's frontmatter.

    MD_PATH is the path to a figmaclaw-rendered page .md file.

    Updates only the `flows:` field in frontmatter. Body is never touched.
    Does not call the Figma API.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    if not md_path.is_absolute():
        md_path = repo_dir / md_path

    try:
        flows: list[list[str]] = json.loads(flows_input)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"--flows: invalid JSON — {exc}") from exc

    md_text = md_path.read_text()
    md_text = _apply_flows(md_text, flows)
    md_path.write_text(md_text)

    rel = str(md_path.relative_to(repo_dir) if md_path.is_relative_to(repo_dir) else md_path)
    click.echo(f"set-flows: updated flows in {rel}")

    if auto_commit and git_commit(repo_dir, [rel], f"sync: set-flows {rel}"):
        click.echo(f"  committed: {rel}")
