"""Smoke tests for claude-run enrichment selection.

These are local integration smoke tests (no network) that exercise the real
CLI path used in CI for selecting files to enrich.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from figmaclaw.main import cli


def _write_page(
    path: Path, *, enriched: bool, placeholder: bool, schema_version: int | None = None
) -> None:
    desc = "(no description yet)" if placeholder else "already described"
    fm = [
        "---",
        "file_key: smoke123",
        'page_node_id: "0:1"',
    ]
    if enriched:
        fm.append('enriched_hash: "sha256:abc"')
    if schema_version is not None:
        fm.append(f"enriched_schema_version: {schema_version}")
    fm.extend(
        [
            "---",
            "",
            "| Screen | Node ID | Description |",
            "|--------|---------|-------------|",
            f"| A | `1:1` | {desc} |",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm))


@pytest.mark.smoke
def test_claude_run_needs_enrichment_backfills_placeholder_pages(tmp_path: Path) -> None:
    """Smoke: enriched_hash pages with placeholders are still queued for enrichment."""
    pages = tmp_path / "figma" / "web-app" / "pages"
    md_placeholder = pages / "legacy-placeholder.md"
    md_clean = pages / "already-enriched.md"
    _write_page(md_placeholder, enriched=True, placeholder=True, schema_version=1)
    _write_page(md_clean, enriched=True, placeholder=False, schema_version=1)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(tmp_path / "figma"),
            "--needs-enrichment",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    out = result.output
    assert str(md_placeholder) in out
    assert str(md_clean) not in out


@pytest.mark.smoke
def test_claude_run_needs_enrichment_excludes_census_files(tmp_path: Path) -> None:
    """Smoke: _census.md must never be queued as an enrichable page."""
    figma_file_dir = tmp_path / "figma" / "design-system-abc123"
    pages = figma_file_dir / "pages"
    md_pending = pages / "pending-page.md"
    _write_page(md_pending, enriched=False, placeholder=True)

    census_md = figma_file_dir / "_census.md"
    census_md.parent.mkdir(parents=True, exist_ok=True)
    census_md.write_text(
        "\n".join(
            [
                "---",
                "file_key: abc123",
                "---",
                "",
                "| Component set | Key | Page | Updated |",
                "|---|---|---|---|",
                "| `Button` | `k1` | Components | 2026-04-15 |",
            ]
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(tmp_path / "figma"),
            "--needs-enrichment",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    out = result.output
    assert str(md_pending) in out
    assert str(census_md) not in out


@pytest.mark.smoke
def test_claude_run_dry_run_also_excludes_census_files(tmp_path: Path) -> None:
    """Smoke: _census.md is excluded even without --needs-enrichment."""
    figma_file_dir = tmp_path / "figma" / "design-system-abc123"
    pages = figma_file_dir / "pages"
    md_page = pages / "pending-page.md"
    _write_page(md_page, enriched=False, placeholder=True)

    census_md = figma_file_dir / "_census.md"
    census_md.parent.mkdir(parents=True, exist_ok=True)
    census_md.write_text(
        "\n".join(
            [
                "---",
                "file_key: abc123",
                "---",
                "",
                "| Component set | Key | Page | Updated |",
                "|---|---|---|---|",
                "| `Button` | `k1` | Components | 2026-04-15 |",
            ]
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "--repo-dir",
            str(tmp_path),
            "claude-run",
            str(tmp_path / "figma"),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    out = result.output
    assert str(md_page) in out
    assert str(census_md) not in out
