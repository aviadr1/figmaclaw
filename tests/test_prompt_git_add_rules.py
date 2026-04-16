from pathlib import Path

from figmaclaw.commands.claude_run import default_prompt_path, finalize_prompt_path


def test_enrichment_prompts_stage_only_target_file() -> None:
    """Enrichment prompts must not stage cache/sync directories."""
    for prompt_path in (default_prompt_path(), finalize_prompt_path()):
        text = prompt_path.read_text(encoding="utf-8")
        assert "git add {file_path}" in text
        assert ".figma-cache/" not in text
        assert ".figma-sync/" not in text


def test_skill_doc_examples_do_not_stage_ignored_cache_paths() -> None:
    """The page enrichment skill should mirror safe staging guidance."""
    skill_path = Path("figmaclaw/skills/figma-enrich-page.md")
    text = skill_path.read_text(encoding="utf-8")
    assert "git add <file_path>" in text
    assert "git add <file_path> .figma-cache/" not in text
    assert "git add <file_path> .figma-sync/" not in text
