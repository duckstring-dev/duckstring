"""The static web UI must be hostable under a path prefix (a reverse proxy, Posit Connect's
/content/{guid}/): every asset reference in the export must be relative, never origin-absolute.
Skipped when the frontend hasn't been built (it's CI-built; locally: ``make build-frontend``)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).parent.parent / "src" / "duckstring" / "catchment" / "static"

pytestmark = pytest.mark.skipif(
    not (_STATIC / "index.html").exists(),
    reason="frontend not built (run `make build-frontend`)",
)


def test_static_export_has_no_absolute_asset_paths():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    absolute = re.findall(r'(?:src|href)="(/[^"]*)"', html)
    assert absolute == [], f"origin-absolute references break subpath hosting: {absolute}"
