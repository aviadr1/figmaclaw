"""Smoke tests against the real Figma API.

Requires FIGMA_API_KEY env var (loaded from repo .env when available). Run with:
    uv run pytest -m smoke_api
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from figmaclaw.commands.build_context import _run as build_context_run
from figmaclaw.figma_client import FigmaClient
from figmaclaw.figma_frontmatter import CURRENT_PULL_SCHEMA_VERSION, FigmaPageFrontmatter
from figmaclaw.figma_models import FigmaPage, FigmaSection, from_page_node
from figmaclaw.figma_parse import parse_frontmatter
from figmaclaw.figma_render import scaffold_page
from figmaclaw.figma_sync_state import FigmaSyncState, PageEntry
from figmaclaw.pull_logic import pull_file
from tests.smoke.live_gate import require_live_credential

# The Web App file used in linear-git
TEST_FILE_KEY = "hOV4QMBnDIG5s5OYkSrX9E"
# A second tracked file that also has the name "Web App" in linear-git manifest.
TEST_FILE_KEY_WEB_APP_DUP = "jb1bZRQUUOQKEpb5p6vt5e"
# Small file with known token sidecars (`cover-0-1`, `screens-1-3`) in linear-git.
TEST_FILE_KEY_LSN_BRANDING = "IXVzan1Xz6J1rA1moyDsk5"
# Reach - auto content sharing page
TEST_PAGE_NODE_ID = "7741:45837"
# Confirmed from live API: 8 SECTION children on this page
EXPECTED_SECTION_COUNT = 8


@pytest.fixture
def api_key() -> str:
    return require_live_credential(
        os.environ.get("FIGMA_API_KEY", ""),
        name="FIGMA_API_KEY",
        hint="Export FIGMA_API_KEY to run real Figma API smoke tests.",
    )


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_get_file_meta_returns_version(api_key: str) -> None:
    """Smoke: get_file_meta hits real API and returns version + pages."""
    async with FigmaClient(api_key=api_key) as client:
        meta = await client.get_file_meta(TEST_FILE_KEY)

    assert meta.version, "version must be non-empty"
    assert meta.lastModified, "lastModified must be non-empty"
    pages = meta.document.children
    assert len(pages) > 0, "file must have at least one page"
    assert all(p.type == "CANVAS" for p in pages)


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_get_page_returns_canvas_with_sections(api_key: str) -> None:
    """Smoke: get_page returns the CANVAS document node directly with SECTION children."""
    async with FigmaClient(api_key=api_key) as client:
        page_node = await client.get_page(TEST_FILE_KEY, TEST_PAGE_NODE_ID)

    assert page_node["type"] == "CANVAS"
    assert page_node["name"] == "Reach - auto content sharing"
    children = page_node["children"]
    assert len(children) > 0
    section_types = {c["type"] for c in children}
    assert "SECTION" in section_types


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_from_page_node_matches_real_api_structure(api_key: str) -> None:
    """Smoke: from_page_node builds a FigmaPage with the correct number of sections.

    INVARIANT: The model structure must match what the real Figma API returns.
    Confirmed from live API: this page has exactly 8 SECTION nodes.
    """
    async with FigmaClient(api_key=api_key) as client:
        meta = await client.get_file_meta(TEST_FILE_KEY)
        file_name = meta.name
        page_node = await client.get_page(TEST_FILE_KEY, TEST_PAGE_NODE_ID)

    page = from_page_node(page_node, file_key=TEST_FILE_KEY, file_name=file_name)

    assert isinstance(page, FigmaPage)
    assert page.file_key == TEST_FILE_KEY
    assert page.file_name == file_name
    assert page.page_node_id == TEST_PAGE_NODE_ID
    assert page.page_name == "Reach - auto content sharing"
    assert len(page.sections) == EXPECTED_SECTION_COUNT, (
        f"Expected {EXPECTED_SECTION_COUNT} sections, got {len(page.sections)}. "
        f"Section names: {[s.name for s in page.sections]}"
    )
    # Every section must have at least one frame
    for section in page.sections:
        assert isinstance(section, FigmaSection)
        assert len(section.frames) > 0, f"Section {section.name!r} has no frames"


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_render_and_parse_round_trip_against_real_page(api_key: str) -> None:
    """Smoke: scaffold_page + parse_frontmatter round-trips correctly for a real Figma page.

    INVARIANT: The YAML frontmatter written by scaffold_page must be parseable
    by parse_frontmatter into a valid FigmaPageFrontmatter with correct identity fields.
    """
    async with FigmaClient(api_key=api_key) as client:
        meta = await client.get_file_meta(TEST_FILE_KEY)
        file_name = meta.name
        page_node = await client.get_page(TEST_FILE_KEY, TEST_PAGE_NODE_ID)

    page = from_page_node(page_node, file_key=TEST_FILE_KEY, file_name=file_name)
    entry = PageEntry(
        page_name=page.page_name,
        page_slug="reach-auto-content-sharing",
        md_path="figma/web-app/pages/reach-auto-content-sharing.md",
        page_hash="smoketest00000000",
        last_refreshed_at="2026-03-31T00:00:00Z",
    )

    md = scaffold_page(page, entry)

    # Must start with frontmatter
    assert md.startswith("---\n"), "Rendered markdown must start with YAML frontmatter"

    # Frontmatter must parse to a valid FigmaPageFrontmatter
    fm = parse_frontmatter(md)
    assert fm is not None, "parse_frontmatter returned None — frontmatter is invalid"
    assert isinstance(fm, FigmaPageFrontmatter)
    assert fm.file_key == TEST_FILE_KEY
    assert fm.page_node_id == TEST_PAGE_NODE_ID

    # Body must have the H1 header and section tables
    assert "# " in md
    assert "| Screen |" in md


@pytest.mark.smoke_webhook
@pytest.mark.asyncio
async def test_list_file_webhooks_returns_list(api_key: str) -> None:
    """Smoke: list_file_webhooks returns a list (may be empty) for a tracked file."""
    async with FigmaClient(api_key=api_key) as client:
        webhooks = await client.list_file_webhooks(file_key=TEST_FILE_KEY)

    assert isinstance(webhooks, list)


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_pull_writes_frame_sections_inventory(tmp_path, api_key: str) -> None:  # type: ignore[no-untyped-def]
    """Smoke: pull_file writes frame_sections with section-level inventory fields."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(TEST_FILE_KEY, "Web App")
    # Force pull to exercise write path even if version is already current.
    state.manifest.files[TEST_FILE_KEY].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        result = await pull_file(client, TEST_FILE_KEY, state, tmp_path, force=False, max_pages=1)

    assert result.pages_written + result.pages_schema_upgraded > 0
    pages = state.manifest.files[TEST_FILE_KEY].pages
    assert pages, "pull_file wrote/upgraded pages but manifest has no page entries"
    entry = next(iter(pages.values()))
    assert entry.md_path is not None

    page_md = tmp_path / entry.md_path
    assert page_md.exists()
    md_text = page_md.read_text()
    fm = parse_frontmatter(md_text)
    assert fm is not None
    assert len(fm.frame_sections) > 0

    any_section = next(iter(fm.frame_sections.values()))[0]
    assert isinstance(any_section.instances, list)
    assert isinstance(any_section.instance_component_ids, list)
    assert isinstance(any_section.raw_count, int)
    assert any_section.raw_count >= 0
    # Assert YAML key presence, not only parser-defaulted fields.
    assert "instances:" in md_text
    assert "instance_component_ids:" in md_text
    assert "raw_count:" in md_text


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_pull_is_idempotent_for_written_page_markdown(tmp_path, api_key: str) -> None:  # type: ignore[no-untyped-def]
    """Smoke: two unchanged pulls produce identical markdown for an already written page."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(TEST_FILE_KEY, "Web App")
    state.manifest.files[TEST_FILE_KEY].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        first = await pull_file(client, TEST_FILE_KEY, state, tmp_path, force=False, max_pages=1)
        if first.pages_errored and first.pages_written + first.pages_schema_upgraded == 0:
            pytest.skip(
                "Figma API returned page errors before live idempotency setup wrote a page"
            )
        assert first.pages_written + first.pages_schema_upgraded > 0

        pages = state.manifest.files[TEST_FILE_KEY].pages
        assert pages, "first pull wrote/upgraded pages but manifest has no entries"
        entry = next(iter(pages.values()))
        assert entry.md_path is not None
        page_md = tmp_path / entry.md_path
        assert page_md.exists()
        md_before = page_md.read_text()
        # First run may be partial (max_pages=1), so explicitly pin schema version to
        # current for idempotency verification on this same page file.
        state.manifest.files[TEST_FILE_KEY].pull_schema_version = CURRENT_PULL_SCHEMA_VERSION
        state.save()

        second = await pull_file(client, TEST_FILE_KEY, state, tmp_path, force=False, max_pages=1)
        if second.pages_errored:
            pytest.skip(
                "Figma API returned page errors during live idempotency verification"
            )
        assert second.pages_written == 0
        assert second.pages_schema_upgraded == 0
        md_after = page_md.read_text()

    assert md_before == md_after


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_pull_collision_safe_file_dirs_and_sidecar_backfill(
    tmp_path,
    api_key: str,  # type: ignore[no-untyped-def]
) -> None:
    """Smoke: same-name files get unique output directories (collision-safe slugs)."""
    state = FigmaSyncState(tmp_path)
    state.load()
    # Intentionally track both as the same name to force slug collision handling.
    state.add_tracked_file(TEST_FILE_KEY, "Web App")
    state.add_tracked_file(TEST_FILE_KEY_WEB_APP_DUP, "Web App")
    state.manifest.files[TEST_FILE_KEY].version = "v0"
    state.manifest.files[TEST_FILE_KEY_WEB_APP_DUP].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        first = await pull_file(client, TEST_FILE_KEY, state, tmp_path, force=False, max_pages=1)
        second = await pull_file(
            client, TEST_FILE_KEY_WEB_APP_DUP, state, tmp_path, force=False, max_pages=1
        )

        assert first.pages_written + first.pages_schema_upgraded > 0
        assert second.pages_written + second.pages_schema_upgraded > 0

        pages_a = state.manifest.files[TEST_FILE_KEY].pages
        pages_b = state.manifest.files[TEST_FILE_KEY_WEB_APP_DUP].pages
        assert pages_a and pages_b

        entry_a = next(iter(pages_a.values()))
        entry_b = next(iter(pages_b.values()))
        assert entry_a.md_path is not None
        assert entry_b.md_path is not None

        assert entry_a.md_path.startswith(f"figma/web-app-{TEST_FILE_KEY}/pages/")
        assert entry_b.md_path.startswith(f"figma/web-app-{TEST_FILE_KEY_WEB_APP_DUP}/pages/")
        assert Path(entry_a.md_path).parts[1] != Path(entry_b.md_path).parts[1]


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_pull_backfills_missing_sidecar_on_unchanged_page_real_api(
    tmp_path,
    api_key: str,  # type: ignore[no-untyped-def]
) -> None:
    """Smoke: deleting a real sidecar is repaired by a subsequent unchanged pull."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(TEST_FILE_KEY_LSN_BRANDING, "LSN Branding")
    state.manifest.files[TEST_FILE_KEY_LSN_BRANDING].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        first = await pull_file(
            client, TEST_FILE_KEY_LSN_BRANDING, state, tmp_path, force=False, max_pages=2
        )
        assert first.pages_written + first.pages_schema_upgraded > 0

        pages = state.manifest.files[TEST_FILE_KEY_LSN_BRANDING].pages
        assert pages
        target_sidecar: Path | None = None
        for entry in pages.values():
            if not entry.md_path:
                continue
            sidecar = (tmp_path / entry.md_path).with_suffix(".tokens.json")
            if "cover-0-1.tokens.json" in str(sidecar):
                continue
            if sidecar.exists():
                target_sidecar = sidecar
                break

        assert target_sidecar is not None, (
            "expected a non-cover sidecar to exist after initial pull"
        )
        target_sidecar.unlink()
        assert not target_sidecar.exists()

        backfill = await pull_file(client, TEST_FILE_KEY_LSN_BRANDING, state, tmp_path, force=False)

    assert backfill.skipped_file is False
    assert target_sidecar.exists()


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_pull_migrates_legacy_sidecar_schema_on_unchanged_page_real_api(
    tmp_path,
    api_key: str,  # type: ignore[no-untyped-def]
) -> None:
    """Smoke: unchanged pages with legacy sidecar schema are rewritten to schema v2."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(TEST_FILE_KEY_LSN_BRANDING, "LSN Branding")
    state.manifest.files[TEST_FILE_KEY_LSN_BRANDING].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        first = await pull_file(
            client, TEST_FILE_KEY_LSN_BRANDING, state, tmp_path, force=False, max_pages=2
        )
        assert first.pages_written + first.pages_schema_upgraded > 0

        pages = state.manifest.files[TEST_FILE_KEY_LSN_BRANDING].pages
        assert pages
        target_sidecar: Path | None = None
        for entry in pages.values():
            if not entry.md_path:
                continue
            sidecar = (tmp_path / entry.md_path).with_suffix(".tokens.json")
            if "cover-0-1.tokens.json" in str(sidecar):
                continue
            if sidecar.exists():
                target_sidecar = sidecar
                break

        assert target_sidecar is not None, (
            "expected a non-cover sidecar to exist after initial pull"
        )

        payload = json.loads(target_sidecar.read_text())
        payload.pop("schema_version", None)
        target_sidecar.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        mutated = json.loads(target_sidecar.read_text())
        assert "schema_version" not in mutated

        migrated = await pull_file(client, TEST_FILE_KEY_LSN_BRANDING, state, tmp_path, force=False)

    assert migrated.skipped_file is False
    rewritten = json.loads(target_sidecar.read_text())
    assert rewritten.get("schema_version") == 2


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_pull_migrates_legacy_unkeyed_paths_to_full_key_slug_real_api(
    tmp_path,
    api_key: str,  # type: ignore[no-untyped-def]
) -> None:
    """Smoke: legacy unkeyed manifest paths are migrated to full-key slug paths."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(TEST_FILE_KEY_LSN_BRANDING, "LSN Branding")
    state.manifest.files[TEST_FILE_KEY_LSN_BRANDING].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        first = await pull_file(
            client, TEST_FILE_KEY_LSN_BRANDING, state, tmp_path, force=False, max_pages=1
        )
        assert first.pages_written + first.pages_schema_upgraded > 0

        pages = state.manifest.files[TEST_FILE_KEY_LSN_BRANDING].pages
        assert pages
        page_id = next(iter(pages.keys()))
        entry = pages[page_id]
        assert entry.md_path is not None

        keyed_rel = entry.md_path
        keyed_abs = tmp_path / keyed_rel
        keyed_sidecar = keyed_abs.with_suffix(".tokens.json")
        assert keyed_abs.exists()
        had_sidecar = keyed_sidecar.exists()

        keyed_dir = Path(keyed_rel).parts[1]
        legacy_dir = keyed_dir.replace(f"-{TEST_FILE_KEY_LSN_BRANDING}", "")
        legacy_rel = str(Path("figma") / legacy_dir / "pages" / Path(keyed_rel).name)
        legacy_abs = tmp_path / legacy_rel
        legacy_abs.parent.mkdir(parents=True, exist_ok=True)
        keyed_abs.rename(legacy_abs)
        if keyed_sidecar.exists():
            keyed_sidecar.rename(legacy_abs.with_suffix(".tokens.json"))

        state.manifest.files[TEST_FILE_KEY_LSN_BRANDING].pages[page_id] = PageEntry(
            page_name=entry.page_name,
            page_slug=entry.page_slug,
            md_path=legacy_rel,
            page_hash=entry.page_hash,
            last_refreshed_at=entry.last_refreshed_at,
            component_md_paths=entry.component_md_paths,
            frame_hashes=entry.frame_hashes,
        )
        state.save()

        migrated = await pull_file(client, TEST_FILE_KEY_LSN_BRANDING, state, tmp_path, force=False)

    assert migrated.skipped_file is False
    assert keyed_abs.exists()
    assert not legacy_abs.exists()
    # If a sidecar existed pre-migration, it must now live at keyed path.
    legacy_sidecar = legacy_abs.with_suffix(".tokens.json")
    if legacy_sidecar.exists():
        raise AssertionError("legacy sidecar path was not pruned")
    if had_sidecar:
        assert keyed_sidecar.exists()


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_schema_upgrade_backfills_instance_component_ids_without_body_rewrite(
    tmp_path,
    api_key: str,  # type: ignore[no-untyped-def]
) -> None:
    """Smoke: schema-stale pull restores missing inventory keys while preserving markdown body."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(TEST_FILE_KEY, "Web App")
    state.manifest.files[TEST_FILE_KEY].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        first = await pull_file(client, TEST_FILE_KEY, state, tmp_path, force=False, max_pages=1)
        assert first.pages_written + first.pages_schema_upgraded > 0
        pages = state.manifest.files[TEST_FILE_KEY].pages
        assert pages
        entry = next(iter(pages.values()))
        assert entry.md_path is not None
        page_md = tmp_path / entry.md_path
        text_before = page_md.read_text()
        body_before = text_before.split("---\n", 2)[-1]

        # Simulate old frontmatter payload by dropping the new stable-ID key lines.
        mutated = re.sub(r"^\s*instance_component_ids:.*\n", "", text_before, flags=re.MULTILINE)
        page_md.write_text(mutated)

        # Mark manifest as pre-v6 so pull runs schema-upgrade path.
        state.manifest.files[TEST_FILE_KEY].pull_schema_version = 5
        state.save()

        upgraded = await pull_file(client, TEST_FILE_KEY, state, tmp_path, force=False, max_pages=1)
        if upgraded.pages_errored:
            pytest.skip(
                "Figma API returned a page error during live schema-upgrade smoke; "
                "body-preservation invariant is inconclusive"
            )
        assert upgraded.pages_written == 0
        assert upgraded.pages_schema_upgraded >= 1

    text_after = page_md.read_text()
    body_after = text_after.split("---\n", 2)[-1]
    assert body_after == body_before
    assert "instance_component_ids:" in text_after


@pytest.mark.smoke_api
@pytest.mark.asyncio
async def test_build_context_generates_valid_call_specs_from_real_pull_data(
    tmp_path,
    api_key: str,  # type: ignore[no-untyped-def]
) -> None:
    """Smoke: build-context generation works against real pulled frame_sections data."""
    state = FigmaSyncState(tmp_path)
    state.load()
    state.add_tracked_file(TEST_FILE_KEY, "Web App")
    state.manifest.files[TEST_FILE_KEY].version = "v0"

    async with FigmaClient(api_key=api_key) as client:
        result = await pull_file(client, TEST_FILE_KEY, state, tmp_path, force=False, max_pages=1)
    if result.pages_errored and result.pages_written + result.pages_schema_upgraded == 0:
        pytest.skip(
            "Figma API returned page errors before live build-context setup wrote a page"
        )
    assert result.pages_written + result.pages_schema_upgraded > 0

    pages = state.manifest.files[TEST_FILE_KEY].pages
    assert pages
    entry = next(iter(pages.values()))
    assert entry.md_path is not None
    source_md = tmp_path / entry.md_path
    fm = parse_frontmatter(source_md.read_text())
    assert fm is not None
    assert fm.frame_sections
    source_frame_id = next(iter(fm.frame_sections.keys()))

    calls = await build_context_run(
        api_key=api_key,
        source_md=source_md,
        source_frame_id=source_frame_id,
        target_file_key="DRAFT_FILE_SMOKE",
        target_page_id="0:0",
        comp_node_id="1:1",
        comp_x=10,
        comp_y=20,
        comp_w=100,
        label="smoke",
    )

    assert isinstance(calls, list)
    assert len(calls) >= 3  # container + >=1 section + caption
    assert "createContextContainer" in calls[0]["code"]
    assert "addContextCaption" in calls[-1]["code"]
    assert all(c["file_key"] == "DRAFT_FILE_SMOKE" for c in calls)
    assert all(len(c["code"]) < 50_000 for c in calls)
