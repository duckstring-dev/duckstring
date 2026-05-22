from __future__ import annotations

import sqlite3

import pytest

from duckstring.catchment.db import connect, migrate


# ---------------------------------------------------------------------------
# DB unit tests — no HTTP layer
# ---------------------------------------------------------------------------

def test_migrate_creates_tables(tmp_path):
    con = connect(tmp_path / "duck.db")
    migrate(con)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pond", "pond_version", "ripple", "ripple_to_ripple", "pond_to_pond",
            "pond_trigger", "demand", "watermark", "pond_run", "ripple_run"} <= tables


def test_migrate_is_idempotent(tmp_path):
    con = connect(tmp_path / "duck.db")
    migrate(con)
    migrate(con)  # second call must not raise or duplicate data
    versions = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == sorted(set(versions))


def test_foreign_keys_enforced(tmp_path):
    con = connect(tmp_path / "duck.db")
    migrate(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO pond_version (pond_id, version, major, source_path) VALUES (999, '1.0.0', 1, 'x')")
        con.commit()


# ---------------------------------------------------------------------------
# HTTP layer tests — use catchment_client fixture from conftest
# ---------------------------------------------------------------------------

def test_health(catchment_client):
    r = catchment_client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_db_available_on_app_state(catchment_client):
    assert catchment_client.app.state.db is not None


def test_root_dir_on_app_state(catchment_client, tmp_path):
    # tmp_path is the same fixture instance shared within the test,
    # but catchment_client uses its own tmp_path injection — just check type.
    assert catchment_client.app.state.root.is_dir()
