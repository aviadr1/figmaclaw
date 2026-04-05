"""Pydantic validation models for Figma REST API responses (figmaclaw#11).

This module owns the **API boundary contract**: every response that
:class:`figmaclaw.figma_client.FigmaClient` returns to its callers is
first parsed into one of the models defined here. Schema drift in the
Figma API — a renamed field, a removed field, a type change — surfaces
as a :class:`FigmaAPIValidationError` at the boundary, with the endpoint
and file_key in the message, instead of propagating as a deep
``KeyError`` or silent ``None`` far from the source.

Design rules:

* **Critical fields are required**: if Figma removes ``name``,
  ``version``, or ``lastModified`` from a file meta response, we fail
  loudly at the client call, not inside a caller that then writes
  ``""`` to the manifest and corrupts state.
* **Optional fields have defaults**: nice-to-have fields that Figma
  occasionally omits (e.g. ``user.handle`` on a version, ``label`` on an
  unlabelled save) default to ``""`` so the model validates cleanly
  when they are absent.
* **Extra fields are ignored** (``extra="ignore"``): if Figma adds a
  new field, we don't break. This is the forward-compat escape hatch.
* **Recursive trees stay as dicts**: the Figma document/canvas tree is
  deeply recursive (FRAME → FRAME → COMPONENT → ...) and
  :func:`figmaclaw.figma_models.from_page_node` already walks it with
  its own conventions. Typing the whole recursion would be a much
  larger refactor and is out of scope for figmaclaw#11. We validate the
  *wrapper* that contains the tree (``NodesResponse``, ``DocumentNode``)
  and keep the recursive children as ``dict[str, Any]``.

See also ``figmaclaw.figma_models`` — that module holds the *internal
domain model* (``FigmaPage``, ``FigmaSection``, ``FigmaFrame``) built
*from* these API responses. Don't confuse the two layers: this module
is the "what Figma sent us" shape; ``figma_models`` is the "what
figmaclaw wants to work with" shape. Separate on purpose.
"""

from __future__ import annotations

from typing import Any

import pydantic

# ---------------------------------------------------------------------------
# Base config and error type
# ---------------------------------------------------------------------------


_BASE_CONFIG = pydantic.ConfigDict(extra="ignore", populate_by_name=True)


class FigmaAPIValidationError(Exception):
    """Raised when a Figma API response fails to validate against its model.

    Wraps :class:`pydantic.ValidationError` with enough context
    (endpoint, file_key or other identifying param) to diagnose schema
    drift without digging through a stack trace.

    The wrapped :class:`pydantic.ValidationError` is preserved on
    ``__cause__`` for callers that want the full structured error.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        context: str,
        inner: pydantic.ValidationError,
    ) -> None:
        self.endpoint = endpoint
        self.context = context
        self.inner = inner
        super().__init__(
            f"Figma API response validation failed: endpoint={endpoint} "
            f"context={context}: {inner.error_count()} error(s) — "
            f"{_summarize_errors(inner)}"
        )


def _summarize_errors(exc: pydantic.ValidationError) -> str:
    """Render a pydantic ValidationError as a one-line summary.

    Format: ``field.path: type (msg); field2.path: type (msg)``.
    Kept short so it fits in a log line; the full exception is still
    available via ``FigmaAPIValidationError.inner`` for deep debugging.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        kind = err.get("type", "?")
        msg = err.get("msg", "")
        parts.append(f"{loc or '<root>'}: {kind} ({msg})")
    return "; ".join(parts)


def _validate(
    model_cls: type[pydantic.BaseModel],
    data: Any,
    *,
    endpoint: str,
    context: str,
) -> Any:
    """Validate *data* against *model_cls*, wrapping errors with context.

    This is the single entry point used by :mod:`figma_client` to build
    typed responses. Keeping the wrapping logic here (rather than at
    every call site) means every API error has the same shape.
    """
    try:
        return model_cls.model_validate(data)
    except pydantic.ValidationError as exc:
        raise FigmaAPIValidationError(
            endpoint=endpoint,
            context=context,
            inner=exc,
        ) from exc


# ---------------------------------------------------------------------------
# GET /v1/files/{file_key}?depth=1  — file metadata + top-level page list
# ---------------------------------------------------------------------------


class CanvasStub(pydantic.BaseModel):
    """A top-level canvas (page) inside the file document tree.

    With ``depth=1`` Figma returns only the canvas nodes themselves
    (no frames). The type is always ``"CANVAS"`` for real pages, but
    Figma sometimes includes other child types at the document level
    (rare, but observed for imported files) — callers should filter by
    ``type == "CANVAS"``.
    """

    model_config = _BASE_CONFIG

    id: str
    name: str
    type: str


class DocumentNode(pydantic.BaseModel):
    """The root document node inside a file metadata response.

    Contains the list of canvas (page) children. We don't type the
    canvas children's own children here — ``depth=1`` returns empty
    children lists anyway, and a full file tree is recursive so it
    stays as :class:`dict`.
    """

    model_config = _BASE_CONFIG

    id: str = ""
    name: str = ""
    type: str = ""
    children: list[CanvasStub] = pydantic.Field(default_factory=list)


class FileMetaResponse(pydantic.BaseModel):
    """Response from ``GET /v1/files/{file_key}?depth=1``.

    Used by ``sync``, ``pull``, and ``track`` to get the current version
    and page list for a file without pulling the full tree. ``name``,
    ``version``, and ``lastModified`` are all required — if any of
    these disappear we want to know immediately.
    """

    model_config = _BASE_CONFIG

    name: str
    version: str
    lastModified: str  # noqa: N815  — matches Figma field name
    document: DocumentNode = pydantic.Field(default_factory=DocumentNode)
    thumbnailUrl: str = ""  # noqa: N815
    role: str = ""
    editorType: str = ""  # noqa: N815
    schemaVersion: int = 0  # noqa: N815

    @property
    def canvas_pages(self) -> list[CanvasStub]:
        """Return only the children that are actual ``CANVAS`` pages.

        Convenience for callers that want to iterate pages without
        re-doing the ``type == "CANVAS"`` filter. Stable across schema
        drift because it's derived, not stored.
        """
        return [c for c in self.document.children if c.type == "CANVAS"]


# ---------------------------------------------------------------------------
# GET /v1/files/{file_key}/nodes?ids={node_id}  — single page tree
# ---------------------------------------------------------------------------


class NodeEntry(pydantic.BaseModel):
    """One entry in the ``nodes`` map of a nodes response.

    The ``document`` field is the recursive Figma node tree (the CANVAS
    and all its descendants). It stays as :class:`dict` because
    :func:`figmaclaw.figma_models.from_page_node` already walks the
    raw tree and building recursive pydantic models would be a much
    larger refactor (out of scope for figmaclaw#11).
    """

    model_config = _BASE_CONFIG

    document: dict[str, Any] | None = None


class NodesResponse(pydantic.BaseModel):
    """Response from ``GET /v1/files/{file_key}/nodes?ids={node_id}``.

    The top-level envelope is typed so callers can validate that the
    ``nodes`` map exists and contains the requested node id. The
    recursive tree inside each entry's ``document`` stays raw.
    """

    model_config = _BASE_CONFIG

    name: str = ""
    lastModified: str = ""  # noqa: N815
    version: str = ""
    nodes: dict[str, NodeEntry] = pydantic.Field(default_factory=dict)


# ---------------------------------------------------------------------------
# GET /v1/teams/{team_id}/projects
# GET /v1/projects/{project_id}/files
# ---------------------------------------------------------------------------


class ProjectSummary(pydantic.BaseModel):
    """One project entry in a team-projects listing.

    Figma returns ``id`` as either a string or an integer depending on
    the endpoint version. We accept both and coerce to string at the
    edges where callers need it (e.g. ``list_project_files(str(p.id))``).
    """

    model_config = _BASE_CONFIG

    id: str | int
    name: str


class TeamProjectsResponse(pydantic.BaseModel):
    """Response from ``GET /v1/teams/{team_id}/projects``."""

    model_config = _BASE_CONFIG

    name: str = ""
    projects: list[ProjectSummary] = pydantic.Field(default_factory=list)


class FileSummary(pydantic.BaseModel):
    """One file entry in a project-files listing.

    Used by ``pull`` and ``list-files`` as a fast listing pre-filter
    (compare ``last_modified`` against the manifest to skip unchanged
    files without fetching meta).
    """

    model_config = _BASE_CONFIG

    key: str
    name: str
    last_modified: str = ""
    thumbnail_url: str = ""


class ProjectFilesResponse(pydantic.BaseModel):
    """Response from ``GET /v1/projects/{project_id}/files``."""

    model_config = _BASE_CONFIG

    name: str = ""
    files: list[FileSummary] = pydantic.Field(default_factory=list)


# ---------------------------------------------------------------------------
# GET /v1/files/{file_key}/versions
# ---------------------------------------------------------------------------


class VersionUser(pydantic.BaseModel):
    """The ``user`` sub-object on a version entry."""

    model_config = _BASE_CONFIG

    id: str | int | None = None
    handle: str = ""
    img_url: str = ""


class VersionSummary(pydantic.BaseModel):
    """One entry in the version history of a file.

    Figma returns ``label`` and ``description`` only for versions that
    were explicitly named by a user; automatic saves have empty
    strings. ``user`` is always present but may have an empty handle.
    """

    model_config = _BASE_CONFIG

    id: str
    created_at: str
    label: str = ""
    description: str = ""
    user: VersionUser = pydantic.Field(default_factory=VersionUser)


class VersionsPage(pydantic.BaseModel):
    """Response from ``GET /v1/files/{file_key}/versions`` (one page).

    ``pagination.next_page`` is followed by the client to fetch more
    pages until the cutoff is reached or max_pages is exhausted.
    """

    model_config = _BASE_CONFIG

    versions: list[VersionSummary] = pydantic.Field(default_factory=list)
    pagination: VersionsPagination | None = None


class VersionsPagination(pydantic.BaseModel):
    """The ``pagination`` sub-object on a versions-list response."""

    model_config = _BASE_CONFIG

    next_page: str | None = None
    prev_page: str | None = None


# Forward-ref rebuild: VersionsPage references VersionsPagination by name.
VersionsPage.model_rebuild()
