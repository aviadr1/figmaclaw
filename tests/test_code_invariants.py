"""Source-level invariant tests.

These tests scan the figmaclaw source tree to enforce structural rules that
cannot be expressed as type constraints.  They run as part of the normal pytest
suite so violations are caught in CI immediately, not via code review.

INVARIANTS:
- All JSON file writes in background pull/sync paths must use write_json_if_changed
  (never raw .write_text(json.dumps(...))) to prevent spurious git commits.
  Exception: user-triggered write commands (suggest_tokens) are intentional and exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

FIGMACLAW_SRC = Path(__file__).parent.parent / "figmaclaw"

# Files that are allowed to call .write_text(json.dumps(...)) directly:
#   figma_utils.py  — the canonical implementation of write_json_if_changed
#   suggest_tokens.py — user-triggered command; always writes by design
_RAW_JSON_WRITE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "figma_utils.py",
        "suggest_tokens.py",
    }
)

_RAW_JSON_WRITE_RE = re.compile(r"\.write_text\s*\(.*?json\.dumps", re.DOTALL)


def test_no_raw_json_writes_outside_allowlist():
    """INVARIANT: background pull/sync code must use write_json_if_changed, not raw
    .write_text(json.dumps(...)).

    Any unconditional JSON write — even just a timestamp field — lands in a git
    commit, triggers Claude enrichment, and wastes CI budget.  The canonical
    utility write_json_if_changed() in figma_utils.py performs the content
    comparison before writing.

    To add a legitimate exception (a user-triggered command that should always
    write), add the filename to _RAW_JSON_WRITE_ALLOWLIST above and document why.
    """
    violations: list[str] = []

    for py_file in sorted(FIGMACLAW_SRC.rglob("*.py")):
        if py_file.name in _RAW_JSON_WRITE_ALLOWLIST:
            continue
        text = py_file.read_text(encoding="utf-8")
        if _RAW_JSON_WRITE_RE.search(text):
            violations.append(str(py_file.relative_to(FIGMACLAW_SRC.parent)))

    assert not violations, (
        "Raw .write_text(json.dumps(...)) found outside the allowlist.\n"
        "Use write_json_if_changed() from figmaclaw.figma_utils instead.\n"
        "Violations:\n" + "\n".join(f"  {v}" for v in violations)
    )
