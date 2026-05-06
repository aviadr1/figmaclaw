from pathlib import Path

from figmaclaw.commands.claude_run import default_prompt_path, finalize_prompt_path

ENRICHMENT_PROMPTS = (
    default_prompt_path(),
    finalize_prompt_path(),
    Path("figmaclaw/prompts/figma-sections-batch.md"),
)


FORBIDDEN_RECOVERY_INSTRUCTIONS = (
    "git push ||",
    "git pull --no-rebase && git push",
)


EXPLICITLY_BANNED_RECOVERY_SNIPPETS = (
    "git stash",
    "git stash pop",
    "git reset --hard",
    "git checkout --",
    "delete `.figma-sync/*`",
)


def test_enrichment_prompts_stage_only_target_file() -> None:
    """Enrichment prompts must not stage cache/sync directories."""
    for prompt_path in ENRICHMENT_PROMPTS:
        text = prompt_path.read_text(encoding="utf-8")
        assert "git add {file_path}" in text
        assert "git add {file_path} .figma-cache/" not in text
        assert "git add {file_path} .figma-sync/" not in text


def test_enrichment_prompts_define_safe_push_recovery_policy() -> None:
    """Prompt guardrail WF-2: prompts stop on rejection instead of merge-retrying."""
    for prompt_path in ENRICHMENT_PROMPTS:
        text = prompt_path.read_text(encoding="utf-8")
        assert "If push is rejected, stop and report the rejected push" in text
        for forbidden in FORBIDDEN_RECOVERY_INSTRUCTIONS:
            assert forbidden not in text
        for forbidden in EXPLICITLY_BANNED_RECOVERY_SNIPPETS:
            assert forbidden in text  # explicitly listed as forbidden in IMPORTANT section


def test_skill_doc_examples_do_not_stage_ignored_cache_paths() -> None:
    """The page enrichment skill should mirror safe staging guidance."""
    skill_path = Path("figmaclaw/skills/figma-enrich-page.md")
    text = skill_path.read_text(encoding="utf-8")
    assert "git add <file_path>" in text
    assert "git add <file_path> .figma-cache/" not in text
    assert "git add <file_path> .figma-sync/" not in text
    assert "git push ||" not in text
    assert "If push is rejected, stop and report the rejected push" in text
