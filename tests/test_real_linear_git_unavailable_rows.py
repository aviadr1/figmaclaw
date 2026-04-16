from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from figmaclaw.commands import screenshots as screenshots_module
from figmaclaw.commands.claude_run import (
    collect_files,
    enrichment_info,
    needs_finalization,
    pending_sections,
)
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_md_parse import parse_sections
from figmaclaw.figma_schema import unresolved_row_node_id
from figmaclaw.main import cli

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "linear_git_real"


@pytest.mark.parametrize(
    ("fixture_name", "expected_unresolved_count"),
    [
        ("showcase_v2_unavailable_rows.md", 20),
        ("live_ui_unavailable_rows.md", 6),
    ],
)
def test_verbatim_linear_git_fixture_rows_stay_pending(
    tmp_path: Path,
    fixture_name: str,
    expected_unresolved_count: int,
) -> None:
    """INVARIANT: unresolved rows in verbatim real fixtures remain pending/retryable."""
    source = _FIXTURE_DIR / fixture_name
    text = source.read_text(encoding="utf-8")

    unresolved_ids = [
        node_id
        for line in text.splitlines()
        if (node_id := unresolved_row_node_id(line)) is not None
    ]
    assert len(unresolved_ids) == expected_unresolved_count

    page = tmp_path / "figma" / "pages" / fixture_name
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")

    needs, frame_count = enrichment_info(page)
    assert needs is True
    assert frame_count >= len(unresolved_ids)

    parsed_body_ids = [f.node_id for s in parse_sections(text) for f in s.frames]
    assert set(unresolved_ids).issubset(set(parsed_body_ids))

    sections = pending_sections(page)
    assert sections
    pending_total = sum(int(s["pending_frames"]) for s in sections)
    assert pending_total == len(unresolved_ids)
    assert needs_finalization(page) is False

    runner = CliRunner()
    inspect_result = runner.invoke(
        cli,
        ["--repo-dir", str(tmp_path), "inspect", str(page), "--json"],
        catch_exceptions=False,
    )
    assert inspect_result.exit_code == 0
    inspect_data = json.loads(inspect_result.output)
    assert inspect_data["missing_descriptions"] == len(unresolved_ids)
    assert inspect_data["pending_sections"] == len(sections)
    assert inspect_data["needs_enrichment"] is True

    selected = collect_files(
        tmp_path / "figma",
        "**/*.md",
        changed_only=False,
        needs_enrichment=True,
    )
    assert selected == [page]


@pytest.mark.asyncio
async def test_screenshots_pending_only_targets_unresolved_rows_in_verbatim_fixture(
    tmp_path: Path,
) -> None:
    """INVARIANT: --pending requests exactly unresolved frame rows from real fixture."""
    source = _FIXTURE_DIR / "showcase_v2_unavailable_rows.md"
    text = source.read_text(encoding="utf-8")

    unresolved_ids = [
        node_id
        for line in text.splitlines()
        if (node_id := unresolved_row_node_id(line)) is not None
    ]
    all_body_ids = [f.node_id for s in parse_sections(text) for f in s.frames]
    expected_pending_ids = [node_id for node_id in all_body_ids if node_id in set(unresolved_ids)]
    assert expected_pending_ids

    page = tmp_path / "figma" / "pages" / source.name
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")

    mock_client = MagicMock(spec=FigmaClient)

    with (
        patch.object(
            screenshots_module,
            "get_image_urls_batched",
            AsyncMock(return_value={node_id: None for node_id in expected_pending_ids}),
        ) as mock_get_image_urls,
        patch.object(screenshots_module, "FigmaClient") as MockClientClass,
    ):
        MockClientClass.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClientClass.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await screenshots_module._run(
            "fake-key",
            tmp_path,
            page,
            pending_only=True,
            stale_only=False,
        )

    args, _kwargs = mock_get_image_urls.call_args
    assert args[2] == expected_pending_ids
    assert result["screenshots"] == []
    assert sorted(result["failed"]) == sorted(expected_pending_ids)
