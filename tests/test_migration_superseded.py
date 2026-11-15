"""Migration 036 rebuilds ``pond_run`` to allow the ``superseded`` status (attention FINDINGS 15). The table is a
foreign-key parent of ``ripple_run``, so the rebuild must keep every row and leave the reference enforced."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from duckstring.catchment import db as catchment_db


def test_036_keeps_runs_and_the_ripple_run_reference(tmp_path, monkeypatch):
    schema = catchment_db._SCHEMA_DIR
    before = tmp_path / "schema"
    before.mkdir()
    for f in sorted(schema.glob("*.sql")):
        if int(f.stem.split("_")[0]) < 36:
            shutil.copy(f, before / f.name)
    con = catchment_db.connect(tmp_path / "duck.db")
    monkeypatch.setattr(catchment_db, "_SCHEMA_DIR", before)
    catchment_db.migrate(con)
    con.execute("PRAGMA foreign_keys = OFF")  # minimal rows, without their parents
    con.execute("INSERT INTO pond_run (pond_version_id, f, status, error, source_pins) "
                "VALUES (1, 'f1', 'failed', 'abandoned', '{}'), (1, 'f2', 'success', NULL, NULL)")
    con.execute("INSERT INTO ripple_run (pond_version_id, f, ripple_id, status) VALUES (1, 'f2', 7, 'success')")
    con.commit()
    con.execute("PRAGMA foreign_keys = ON")

    monkeypatch.setattr(catchment_db, "_SCHEMA_DIR", schema)
    catchment_db.migrate(con)
    rows = con.execute("SELECT f, status, error, source_pins, superseded_by FROM pond_run ORDER BY f").fetchall()
    assert rows == [("f1", "failed", "abandoned", "{}", None), ("f2", "success", None, None, None)]  # history kept
    assert con.execute("SELECT count(*) FROM ripple_run").fetchall() == [(1,)]
    con.execute("UPDATE pond_run SET status = 'superseded', superseded_by = 'f2' WHERE f = 'f1'")
    assert con.execute("PRAGMA foreign_keys").fetchall() == [(1,)]
    assert any(fk[2] == "pond_run" for fk in con.execute("PRAGMA foreign_key_list(ripple_run)").fetchall())
    with pytest.raises(sqlite3.IntegrityError):  # still enforced
        con.execute("INSERT INTO ripple_run (pond_version_id, f, ripple_id, status) VALUES (1, 'nope', 7, 'success')")
    con.rollback()  # PRAGMA foreign_keys is a no-op inside the transaction the failed insert opened
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("INSERT INTO ripple_run (pond_version_id, f, ripple_id, status) VALUES (1, 'nope', 7, 'success')")
    parents = {row[2] for row in con.execute("PRAGMA foreign_key_check(ripple_run)").fetchall()}
    assert "pond_run" in parents  # the orphan is reported against the new pond_run, by name
