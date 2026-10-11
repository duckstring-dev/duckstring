"""Shared fixtures for the Trickle engine's tests."""

from __future__ import annotations

import duckdb
import pytest


@pytest.fixture
def db(tmp_path):
    """One database holding the sources and the outputs, as a standalone user has."""
    con = duckdb.connect(str(tmp_path / "db.duckdb"))
    yield con
    con.close()
