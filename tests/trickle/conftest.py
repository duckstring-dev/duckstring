"""The Trickle engine's own tests (plans/trickle-extraction.md). They use only the engine and its standalone
host, and import it as ``trickle``, the name it has once published, so they move to the package unchanged.
Until then this aliases ``duckstring.trickle`` as ``trickle``; it is the only file here allowed to import
Duckstring (``tests/test_trickle_isolation.py`` checks)."""

from __future__ import annotations

import importlib
import sys

import duckdb
import pytest

import duckstring.trickle as _trickle

sys.modules.setdefault("trickle", _trickle)
for _name in ("acc", "agg", "builder", "capture", "context", "io", "lineage", "session"):
    sys.modules.setdefault(f"trickle.{_name}", importlib.import_module(f"duckstring.trickle.{_name}"))


@pytest.fixture
def db(tmp_path):
    """One database holding the sources and the outputs, as a standalone user has."""
    con = duckdb.connect(str(tmp_path / "db.duckdb"))
    yield con
    con.close()
