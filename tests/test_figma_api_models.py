"""Tests for figmaclaw.figma_api_models (figmaclaw#11).

These tests pin the API boundary contract. They exercise:

1. **Happy path** — realistic fixture responses parse cleanly.
2. **Forward compat** — Figma adding unknown fields does NOT break
   parsing (``extra="ignore"``).
3. **Backward compat** — old responses missing optional fields default
   sensibly.
4. **Schema drift surfaces loudly** — removing a *critical* field
   raises :class:`FigmaAPIValidationError` with enough context to
   diagnose without digging through a stack trace.

INVARIANT: the ``FigmaAPIValidationError`` message format is part of
the contract. CI log consumers grep for "Figma API response validation
failed"; tests pin that exact string.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pydantic
import pytest

from figmaclaw.figma_api_models import (
    FigmaAPIValidationError,
    FileMetaResponse,
    FileSummary,
    NodesResponse,
    ProjectFilesResponse,
    ProjectSummary,
    TeamProjectsResponse,
    VersionsPage,
    VersionSummary,
    _summarize_errors,
    _validate,
)


class TestSyntaxValidity:
    """Canary: broken api_models module silently disables the whole CLI."""

    def test_module_compiles(self) -> None:
        script = Path(__file__).parent.parent / "figmaclaw" / "figma_api_models.py"
        py_compile.compile(str(script), doraise=True)


# ---------------------------------------------------------------------------
# GET /v1/files/{file_key}?depth=1  — FileMetaResponse
# ---------------------------------------------------------------------------


class TestFileMetaResponse:
    """Happy path, schema drift, and the canvas_pages convenience."""

    HAPPY_PATH: dict = {
        "name": "Design System",
        "version": "4567890123",
        "lastModified": "2026-04-05T12:34:56Z",
        "thumbnailUrl": "https://s3.../thumb.png",
        "role": "owner",
        "editorType": "figma",
        "schemaVersion": 0,
        "document": {
            "id": "0:0",
            "name": "Document",
            "type": "DOCUMENT",
            "children": [
                {"id": "0:1", "name": "Cover", "type": "CANVAS"},
                {"id": "0:2", "name": "Components", "type": "CANVAS"},
                # Figma occasionally includes non-canvas children at the doc level
                {"id": "0:3", "name": "Comment", "type": "TEXT"},
            ],
        },
    }

    def test_happy_path_parses(self) -> None:
        meta = FileMetaResponse.model_validate(self.HAPPY_PATH)
        assert meta.name == "Design System"
        assert meta.version == "4567890123"
        assert meta.lastModified == "2026-04-05T12:34:56Z"
        assert meta.document.id == "0:0"
        assert len(meta.document.children) == 3
        assert meta.document.children[0].name == "Cover"
        assert meta.document.children[0].type == "CANVAS"

    def test_canvas_pages_filters_non_canvas_children(self) -> None:
        """canvas_pages is the idiomatic way for callers to iterate pages."""
        meta = FileMetaResponse.model_validate(self.HAPPY_PATH)
        pages = meta.canvas_pages
        assert len(pages) == 2
        assert all(p.type == "CANVAS" for p in pages)
        assert [p.name for p in pages] == ["Cover", "Components"]

    def test_extra_fields_ignored_forward_compat(self) -> None:
        """Figma adding a new field must not break figmaclaw."""
        data = {**self.HAPPY_PATH, "futureField": "surprise"}
        data["document"]["bonusField"] = 42
        data["document"]["children"][0]["newThing"] = {"nested": "value"}
        meta = FileMetaResponse.model_validate(data)
        assert meta.name == "Design System"
        assert meta.document.children[0].id == "0:1"

    def test_missing_name_raises_loudly(self) -> None:
        """name is a critical field — must fail at the boundary."""
        data = {k: v for k, v in self.HAPPY_PATH.items() if k != "name"}
        with pytest.raises(pydantic.ValidationError):
            FileMetaResponse.model_validate(data)

    def test_missing_version_raises_loudly(self) -> None:
        data = {k: v for k, v in self.HAPPY_PATH.items() if k != "version"}
        with pytest.raises(pydantic.ValidationError):
            FileMetaResponse.model_validate(data)

    def test_missing_lastmodified_raises_loudly(self) -> None:
        data = {k: v for k, v in self.HAPPY_PATH.items() if k != "lastModified"}
        with pytest.raises(pydantic.ValidationError):
            FileMetaResponse.model_validate(data)

    def test_missing_document_defaults_to_empty(self) -> None:
        """document is optional — a file with no pages is legal."""
        data = {k: v for k, v in self.HAPPY_PATH.items() if k != "document"}
        meta = FileMetaResponse.model_validate(data)
        assert meta.document.children == []
        assert meta.canvas_pages == []

    def test_canvas_stub_missing_id_raises(self) -> None:
        """A malformed canvas child surfaces at the boundary, not deep inside."""
        data = {
            **self.HAPPY_PATH,
            "document": {
                "id": "0:0",
                "children": [{"name": "No ID!", "type": "CANVAS"}],
            },
        }
        with pytest.raises(pydantic.ValidationError):
            FileMetaResponse.model_validate(data)


# ---------------------------------------------------------------------------
# GET /v1/files/{file_key}/nodes?ids={node_id}  — NodesResponse
# ---------------------------------------------------------------------------


class TestNodesResponse:
    """Wrapper validation; the recursive document tree stays as dict."""

    HAPPY_PATH: dict = {
        "name": "Design System",
        "lastModified": "2026-04-05T12:34:56Z",
        "version": "4567890123",
        "nodes": {
            "0:1": {
                "document": {
                    "id": "0:1",
                    "name": "Home",
                    "type": "CANVAS",
                    "children": [
                        {"id": "1:1", "name": "Hero Frame", "type": "FRAME", "children": []},
                    ],
                },
                "components": {},
                "styles": {},
            },
        },
    }

    def test_happy_path_parses(self) -> None:
        resp = NodesResponse.model_validate(self.HAPPY_PATH)
        assert "0:1" in resp.nodes
        entry = resp.nodes["0:1"]
        # document stays as dict — callers pass it to from_page_node as-is
        assert isinstance(entry.document, dict)
        assert entry.document["id"] == "0:1"
        assert entry.document["children"][0]["name"] == "Hero Frame"

    def test_empty_nodes_map_is_legal(self) -> None:
        """When the requested id isn't in the file, Figma returns nodes={}."""
        data = {"nodes": {}}
        resp = NodesResponse.model_validate(data)
        assert resp.nodes == {}

    def test_missing_document_in_entry(self) -> None:
        """A node entry with null document is accepted (defensive)."""
        data = {"nodes": {"0:1": {"document": None}}}
        resp = NodesResponse.model_validate(data)
        assert resp.nodes["0:1"].document is None

    def test_extra_fields_ignored(self) -> None:
        data = {
            **self.HAPPY_PATH,
            "mysteryTopLevel": "???",
            "nodes": {
                "0:1": {
                    **self.HAPPY_PATH["nodes"]["0:1"],
                    "mysteryEntryField": [1, 2, 3],
                },
            },
        }
        resp = NodesResponse.model_validate(data)
        assert "0:1" in resp.nodes


# ---------------------------------------------------------------------------
# Team projects / project files listings
# ---------------------------------------------------------------------------


class TestTeamProjectsResponse:
    def test_happy_path(self) -> None:
        data = {
            "name": "Engineering",
            "projects": [
                {"id": "12345", "name": "Mobile App"},
                {"id": 67890, "name": "Web Portal"},  # int id
            ],
        }
        resp = TeamProjectsResponse.model_validate(data)
        assert len(resp.projects) == 2
        assert resp.projects[0].id == "12345"
        assert resp.projects[1].id == 67890  # int preserved

    def test_project_id_accepts_both_str_and_int(self) -> None:
        """Figma is inconsistent about project id type — accept both."""
        assert ProjectSummary.model_validate({"id": "abc", "name": "A"}).id == "abc"
        assert ProjectSummary.model_validate({"id": 42, "name": "B"}).id == 42

    def test_missing_project_name_raises(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ProjectSummary.model_validate({"id": "1"})

    def test_empty_projects_list(self) -> None:
        resp = TeamProjectsResponse.model_validate({"projects": []})
        assert resp.projects == []


class TestProjectFilesResponse:
    def test_happy_path(self) -> None:
        data = {
            "name": "Mobile App",
            "files": [
                {
                    "key": "abcDEF123",
                    "name": "iOS Home",
                    "last_modified": "2026-04-05T12:00:00Z",
                    "thumbnail_url": "https://s3.../t.png",
                },
                {"key": "xyz789", "name": "Android Home"},  # missing optional fields
            ],
        }
        resp = ProjectFilesResponse.model_validate(data)
        assert len(resp.files) == 2
        assert resp.files[0].last_modified == "2026-04-05T12:00:00Z"
        assert resp.files[1].last_modified == ""  # defaulted

    def test_missing_key_raises(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FileSummary.model_validate({"name": "No key"})

    def test_missing_name_raises(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FileSummary.model_validate({"key": "abc"})


# ---------------------------------------------------------------------------
# Versions listing
# ---------------------------------------------------------------------------


class TestVersionsPage:
    HAPPY_PATH: dict = {
        "versions": [
            {
                "id": "v1",
                "created_at": "2026-04-05T12:00:00Z",
                "label": "Release 1.0",
                "description": "First stable",
                "user": {"id": "u1", "handle": "alice", "img_url": "https://.../a.png"},
            },
            {
                "id": "v2",
                "created_at": "2026-04-04T10:00:00Z",
                # No label / description / user.handle — common for autosaves
                "user": {"id": None},
            },
        ],
        "pagination": {"next_page": "/v1/files/abc/versions?cursor=xxx"},
    }

    def test_happy_path(self) -> None:
        page = VersionsPage.model_validate(self.HAPPY_PATH)
        assert len(page.versions) == 2
        v1, v2 = page.versions
        assert v1.label == "Release 1.0"
        assert v1.user.handle == "alice"
        assert v2.label == ""  # defaulted
        assert v2.user.handle == ""  # defaulted
        assert v2.user.id is None
        assert page.pagination is not None
        assert page.pagination.next_page is not None
        assert page.pagination.next_page.startswith("/v1/files/")

    def test_missing_pagination_is_legal(self) -> None:
        """Last page of results has no pagination block."""
        data = {"versions": []}
        page = VersionsPage.model_validate(data)
        assert page.pagination is None

    def test_version_missing_id_raises(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            VersionSummary.model_validate({"created_at": "2026-04-05T12:00:00Z"})

    def test_version_user_completely_missing_defaults(self) -> None:
        """A version with no user block still validates — defaults to empty user."""
        v = VersionSummary.model_validate({"id": "v1", "created_at": "2026-04-05T12:00:00Z"})
        assert v.user.handle == ""


# ---------------------------------------------------------------------------
# FigmaAPIValidationError wrapper — the single boundary error shape
# ---------------------------------------------------------------------------


class TestValidationErrorWrapper:
    """``_validate`` is the one function figma_client uses to build responses.

    The wrapper converts pydantic's ValidationError into a
    FigmaAPIValidationError with endpoint and context baked into the
    message, so log consumers can find the failing call without reading
    a stack trace.
    """

    def test_wraps_error_with_endpoint_and_context(self) -> None:
        bad = {"name": "x"}  # missing version and lastModified
        with pytest.raises(FigmaAPIValidationError) as exc_info:
            _validate(
                FileMetaResponse,
                bad,
                endpoint="GET /v1/files/{key}",
                context="file_key=abcDEF123",
            )
        err = exc_info.value
        assert err.endpoint == "GET /v1/files/{key}"
        assert err.context == "file_key=abcDEF123"
        assert isinstance(err.inner, pydantic.ValidationError)
        assert "Figma API response validation failed" in str(err)
        assert "endpoint=GET /v1/files/{key}" in str(err)
        assert "context=file_key=abcDEF123" in str(err)
        # The __cause__ chain preserves the original ValidationError
        assert err.__cause__ is err.inner

    def test_error_summary_includes_missing_field_names(self) -> None:
        bad = {"name": "x"}
        with pytest.raises(FigmaAPIValidationError) as exc_info:
            _validate(
                FileMetaResponse,
                bad,
                endpoint="GET /v1/files/{key}",
                context="file_key=abc",
            )
        msg = str(exc_info.value)
        assert "version" in msg
        assert "lastModified" in msg

    def test_valid_input_passes_through(self) -> None:
        good = {
            "name": "x",
            "version": "v",
            "lastModified": "2026-04-05T12:00:00Z",
        }
        result = _validate(
            FileMetaResponse,
            good,
            endpoint="GET /v1/files/{key}",
            context="file_key=abc",
        )
        assert isinstance(result, FileMetaResponse)
        assert result.name == "x"

    def test_summarize_errors_is_stable_format(self) -> None:
        try:
            FileMetaResponse.model_validate({})
        except pydantic.ValidationError as exc:
            summary = _summarize_errors(exc)
            # Should mention each missing field with its loc and type
            assert "name" in summary
            assert "version" in summary
            assert "lastModified" in summary
            assert "missing" in summary  # pydantic's error type for required fields
