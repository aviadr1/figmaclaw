"""Guardrail tests for ``figmaclaw.figma_md_parse``.

These tests enforce the two-layer design between :mod:`figma_schema`
(single-line primitives, regex patterns, format constants) and
:mod:`figma_md_parse` (multi-line document parsing). They exist so that
future code changes cannot silently re-introduce the drift that caused
figmaclaw#25 — where two separate modules held unilateral, unsynchronized
opinions about the section heading format.

The rules enforced here:

* ``figma_md_parse`` does NOT import the ``re`` module. All regex lives
  in ``figma_schema``.
* ``figma_md_parse`` does NOT define its own regex patterns (no
  ``re.compile`` calls even through a different import path).
* ``figma_md_parse`` does NOT contain string literals for the canonical
  schema constants (``(Unnamed)``, ``(Ungrouped)``, ``(no description
  yet)``, etc.). It imports them from ``figma_schema``.
* ``figma_md_parse`` DOES import from ``figma_schema`` — otherwise it's
  not reusing the primitives at all.

When one of these tests fails, the remedy is: move the new primitive
to ``figma_schema`` and import it from here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_MD_PARSE_PATH = Path(__file__).parent.parent / "figmaclaw" / "figma_md_parse.py"


def _load_ast() -> ast.Module:
    return ast.parse(_MD_PARSE_PATH.read_text())


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


# ---------------------------------------------------------------------------
# GR-1: no regex module imported.
# ---------------------------------------------------------------------------


def test_figma_md_parse_does_not_import_re() -> None:
    """Regex primitives live in ``figma_schema``; this module must not
    import ``re`` directly. If you need a new regex, add it there."""
    modules = _imported_modules(_load_ast())
    assert "re" not in modules, (
        "figma_md_parse must not import 're' — regex primitives belong in "
        "figma_schema. Move the pattern there and import the parser function."
    )


# ---------------------------------------------------------------------------
# GR-2: no ad-hoc regex compilation.
# ---------------------------------------------------------------------------


def test_figma_md_parse_has_no_regex_compile_calls() -> None:
    """Even through indirect imports, there must be no ``.compile(...)``
    call in this module. The presence of one is a strong signal that
    schema knowledge is being duplicated."""
    tree = _load_ast()
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name == "compile":
                offenders.append(f"line {getattr(node, 'lineno', '?')}: {ast.unparse(node)}")
    assert not offenders, (
        "figma_md_parse must not call .compile() — regex patterns belong "
        "in figma_schema. Found:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# GR-3: no inline literals for canonical schema constants.
# ---------------------------------------------------------------------------


# Canonical constants that live in figma_schema. If any of these string
# literals appears in figma_md_parse.py, the author probably copy-pasted
# from the schema instead of importing — which is exactly how the
# figmaclaw#25 drift started.
_FORBIDDEN_LITERALS: tuple[str, ...] = (
    '"(no description yet)"',
    "'(no description yet)'",
    '"(Unnamed)"',
    "'(Unnamed)'",
    '"(Ungrouped)"',
    "'(Ungrouped)'",
    '"## Screen Flow"',
    "'## Screen Flow'",
    '"| Screen | Node ID | Description |"',
    "'| Screen | Node ID | Description |'",
)


def test_figma_md_parse_has_no_inline_schema_constants() -> None:
    """Any canonical schema constant must be imported from figma_schema,
    never inlined as a string literal. A literal copy is a tripwire for
    format drift."""
    source = _MD_PARSE_PATH.read_text()
    offenders = [lit for lit in _FORBIDDEN_LITERALS if lit in source]
    assert not offenders, (
        "figma_md_parse contains canonical schema literals that must "
        "instead be imported from figma_schema:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# GR-4: must actively import from figma_schema (not drift into a silo).
# ---------------------------------------------------------------------------


def test_figma_md_parse_imports_from_figma_schema() -> None:
    """The whole point of this module is to build on schema primitives.
    If it stops importing from figma_schema, someone has re-implemented
    the primitives locally — which defeats the two-layer design."""
    modules = _imported_modules(_load_ast())
    assert "figmaclaw.figma_schema" in modules, (
        "figma_md_parse must import from figma_schema — without that "
        "import the two-layer design is broken and code is probably "
        "duplicating schema knowledge."
    )


# ---------------------------------------------------------------------------
# GR-5: public surface area is pinned — new exports require a conscious
# decision, not an accidental one.
# ---------------------------------------------------------------------------


_EXPECTED_PUBLIC_NAMES: frozenset[str] = frozenset(
    {
        "ParsedFrame",
        "ParsedSection",
        "section_line_ranges",
        "parse_sections",
        "frame_row_count",
    }
)


def test_figma_md_parse_public_surface_is_stable() -> None:
    """The set of public names exported by figma_md_parse is intentionally
    small. Adding a new public name should be a deliberate act that
    updates this test — not something that slips in unnoticed.

    Private names (starting with ``_``) are unrestricted.
    """
    tree = _load_ast()
    public: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef) and not node.name.startswith("_"):
            public.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    public.add(target.id)

    missing = _EXPECTED_PUBLIC_NAMES - public
    extra = public - _EXPECTED_PUBLIC_NAMES

    assert not missing, (
        f"Expected public names missing from figma_md_parse: {sorted(missing)}. "
        f"If you removed one intentionally, update _EXPECTED_PUBLIC_NAMES in "
        f"this test."
    )
    assert not extra, (
        f"figma_md_parse exports unexpected public names: {sorted(extra)}. "
        f"If you added one intentionally, update _EXPECTED_PUBLIC_NAMES. "
        f"Otherwise, consider whether it belongs in figma_schema instead."
    )
