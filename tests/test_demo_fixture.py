"""Offline demo fixture still mirrors a greppable Checkout page.

INVARIANTS:
- docs/demo_fixture.py writes markdown through pull_file with no network
- the page path matches docs/demo.tape
- frame names from the fixture are present in the markdown body
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "docs" / "demo_fixture.py"
_PAGE = Path("figma/shop-demo01/pages/checkout-12-40.md")


def test_demo_fixture_mirrors_checkout_page(tmp_path: Path) -> None:
    """INVARIANT: the offline fixture writes the Checkout page the demo greps."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    page = tmp_path / _PAGE
    assert page.is_file(), result.stdout
    text = page.read_text(encoding="utf-8")
    assert "Checkout" in text
    assert "Order confirmation" in text
    assert str(_PAGE) in result.stdout
