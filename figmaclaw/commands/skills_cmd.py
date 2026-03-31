"""figmaclaw skills — list and install agent skills from the figmaclaw marketplace.

Skills are markdown files bundled with figmaclaw that provide AI agents with
instructions for specific workflows (e.g. enriching a Figma page .md file).

Installing a skill copies it into the consuming repo's .agents/skills/ directory,
where it becomes available to Claude Code and compatible agents automatically.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

_SKILLS_DIR = Path(__file__).parent.parent / "skills"


@click.group("skills")
def skills_group() -> None:
    """List and install agent skills from the figmaclaw marketplace."""


@skills_group.command("list")
def skills_list_cmd() -> None:
    """List all skills available in the figmaclaw marketplace."""
    skill_files = sorted(_SKILLS_DIR.glob("*.md"))
    if not skill_files:
        click.echo("No skills available.")
        return
    for f in skill_files:
        click.echo(f.stem)


@skills_group.command("install")
@click.argument("skill_name", required=False)
@click.option("--all", "install_all", is_flag=True, help="Install all available skills.")
@click.pass_context
def skills_install_cmd(
    ctx: click.Context,
    skill_name: str | None,
    install_all: bool,
) -> None:
    """Install a skill into the repo's .agents/skills/ directory.

    SKILL_NAME is the name of the skill to install (without .md extension).
    Use --all to install every skill in the marketplace.
    """
    repo_dir = Path(ctx.obj["repo_dir"])
    target_dir = repo_dir / ".agents" / "skills"
    target_dir.mkdir(parents=True, exist_ok=True)

    if install_all:
        skill_files = sorted(_SKILLS_DIR.glob("*.md"))
        if not skill_files:
            raise click.ClickException("No skills available in the marketplace.")
    elif skill_name:
        src = _SKILLS_DIR / f"{skill_name}.md"
        if not src.exists():
            available = [f.stem for f in sorted(_SKILLS_DIR.glob("*.md"))]
            hint = f"Available: {', '.join(available)}" if available else "No skills available."
            raise click.ClickException(f"Skill '{skill_name}' not found. {hint}")
        skill_files = [src]
    else:
        raise click.UsageError("Provide a SKILL_NAME or use --all.")

    for src in skill_files:
        dst = target_dir / src.name
        shutil.copy2(src, dst)
        rel = dst.relative_to(repo_dir) if dst.is_relative_to(repo_dir) else dst
        click.echo(f"installed: {src.stem} → {rel}")
