"""figmaclaw self — introspection commands for the figmaclaw CLI itself.

figmaclaw self skill [NAME]
  Print the bundled Claude Code skill file for figmaclaw.
  Useful for agents that need to know how to use figmaclaw correctly.
"""

from __future__ import annotations

from pathlib import Path

import click

_SKILLS_DIR = Path(__file__).parent.parent / "skills"


@click.group("self")
def self_group() -> None:
    """Introspection commands — print bundled skills and documentation."""


@self_group.command("skill")
@click.argument("name", default=None, required=False)
@click.option("--list", "list_skills", is_flag=True, help="List available skill names.")
def skill_cmd(name: str | None, list_skills: bool) -> None:
    """Print a bundled Claude Code skill file.

    NAME is the skill name without the .md extension (e.g. enrich-page).
    Defaults to printing all skills if NAME is omitted.

    Examples:

    \b
      figmaclaw self skill
      figmaclaw self skill enrich-page
      figmaclaw self skill --list
    """
    if not _SKILLS_DIR.exists():
        raise click.ClickException(f"Skills directory not found: {_SKILLS_DIR}")

    skill_files = sorted(_SKILLS_DIR.glob("*.md"))
    if not skill_files:
        raise click.ClickException(f"No skill files found in {_SKILLS_DIR}")

    if list_skills:
        for f in skill_files:
            click.echo(f.stem)
        return

    if name is not None:
        path = _SKILLS_DIR / f"{name}.md"
        if not path.exists():
            available = ", ".join(f.stem for f in skill_files)
            raise click.ClickException(f"Skill {name!r} not found. Available: {available}")
        click.echo(path.read_text(), nl=False)
        return

    # No name given — print all skills separated by a header
    for i, f in enumerate(skill_files):
        if i > 0:
            click.echo("\n---\n")
        click.echo(f.read_text(), nl=False)
