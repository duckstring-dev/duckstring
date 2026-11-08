"""The Databricks SQL Flock engine and its REST client, offline (plans/databricks-bigquery.md, parts 2 and 3).

A fake workspace (an httpx mock transport) stands in for Databricks: the Files API keeps uploads in memory, and the
Statement Execution API translates the statement back to DuckDB with sqlglot (``read_files`` over a Volume path
becoming ``read_parquet`` over the uploads), runs it, and returns the result as Arrow chunks behind presigned links.
That tests staging, upload, the statement lifecycle, chunked results, empty results and cleanup. What a real
warehouse is trusted with is measured in ``test_flock_databricks_conformance.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import httpx
import pytest
from databricks_fake import HOST, FakeWorkspace

from duckstring import databricks_rest as dbx
from duckstring import flock
from duckstring import trickle_io as T
from duckstring.core import Pond

pytest.importorskip("sqlglot")
pytestmark = pytest.mark.timeout(30)

UTC = timezone.utc


def _engine(ws, **env):
    from duckstring.flock.engines.databricks import DatabricksEngine

    e = DatabricksEngine({"DUCKSTRING_FLOCK_DATABRICKS_HOST": HOST, "DUCKSTRING_FLOCK_DATABRICKS_WAREHOUSE": "wh1",
                          "DUCKSTRING_FLOCK_DATABRICKS_VOLUME": "/Volumes/main/flock/scratch/",
                          "DATABRICKS_TOKEN": "pat-1", **env})
    e.transport = ws.transport()
    e.measuring = True  # everything is sent; the profile is measured against a real warehouse
    return e


def _pond(tmp_path, rows=6):
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    pond = Pond("p", "1", con, root=tmp_path, f=datetime(2026, 11, 8, tzinfo=UTC), flock="off")
    T.merge_table(con, "t", con.sql(f"SELECT range AS id, range % 3 AS g, range * 1.5 AS x FROM range({rows})"),
                  pond.f, ("id",))
    return pond


def _job(pond, select="s0.id, s0.g, s0.x * 2 AS y"):
    from trickle.builder import ComprehensiveJob

    return ComprehensiveJob(pond.trickle("t").select(select), ("id",))


def test_a_job_runs_on_the_warehouse_and_comes_back_in_chunks(tmp_path):
    ws = FakeWorkspace(chunk_rows=2)
    engine, pond = _engine(ws), _pond(tmp_path)
    job = _job(pond)
    result = flock.conform(job, engine.dispatch(job))
    assert sorted(result.fetchall()) == sorted(job.relation().fetchall())
    assert "read_files('/Volumes/main/flock/scratch/" in ws.executed[0]
    assert len(ws.statements["s0"]["_links"]) == 3  # six rows in chunks of two, fetched past the first link
    assert not ws.files  # the staged sources were removed from the Volume
    assert set(ws.auth) == {"Bearer pat-1"}


def test_an_empty_result_keeps_its_columns(tmp_path):
    ws = FakeWorkspace()
    engine, pond = _engine(ws), _pond(tmp_path)
    from trickle.builder import ComprehensiveJob

    job = ComprehensiveJob(pond.trickle("t").filter("s0.id < 0").select("s0.id, s0.g"), ("id",))
    result = flock.conform(job, engine.dispatch(job))
    assert result is not None and result.fetchall() == []
    assert [c.lower() for c in result.columns] == ["id", "g"]


def test_a_failed_statement_falls_back_to_local(tmp_path):
    ws = FakeWorkspace()
    engine, pond = _engine(ws), _pond(tmp_path)
    job = _job(pond)
    ws._run = lambda body: httpx.Response(200, json={"statement_id": "bad", "status": {
        "state": "FAILED", "error": {"message": "[UNRESOLVED_COLUMN] boom"}}})
    assert engine.dispatch(job) is None
    assert "UNRESOLVED_COLUMN" in engine.last_error
    assert not ws.files


def test_large_sources_are_staged_as_several_files(tmp_path, monkeypatch):
    from duckstring.flock.engines.databricks import DatabricksEngine

    ws = FakeWorkspace(chunk_rows=10_000)
    monkeypatch.setattr(DatabricksEngine, "STAGE_FILE_BYTES", 200_000)
    engine, pond = _engine(ws), _pond(tmp_path, rows=200_000)
    seen = []
    put = ws.handle

    def spy(request):
        if request.method == "PUT":
            seen.append(request.url.path)
        return put(request)

    engine.transport = httpx.MockTransport(spy)
    job = _job(pond)
    result = flock.conform(job, engine.dispatch(job))
    assert result.count("*").fetchall()[0][0] == 200_000
    assert len(seen) > 1 and all(p.endswith(".parquet") for p in seen)


def test_oauth_m2m_is_used_without_a_token_and_cached(tmp_path):
    ws = FakeWorkspace()
    client = dbx.Client(HOST, {"DATABRICKS_CLIENT_ID": "cid", "DATABRICKS_CLIENT_SECRET": "sec"},
                        transport=ws.transport())
    client.list_dir("/Volumes/a/b/c")
    client.list_dir("/Volumes/a/b/c")
    assert ws.auth == ["Bearer m2m-token", "Bearer m2m-token"]


def test_no_credentials_is_a_clear_error_off_azure():
    client = dbx.Client("https://dbc-1.cloud.databricks.com", {}, transport=FakeWorkspace().transport())
    with pytest.raises(dbx.DatabricksError, match="no Databricks credentials"):
        client.list_dir("/Volumes/a/b/c")


def test_the_engine_is_enabled_by_host_warehouse_and_volume():
    from duckstring.flock.engines.databricks import DatabricksEngine

    assert not DatabricksEngine({}).enabled()
    assert DatabricksEngine({"DATABRICKS_HOST": HOST, "DUCKSTRING_FLOCK_DATABRICKS_WAREHOUSE": "w",
                             "DUCKSTRING_FLOCK_DATABRICKS_VOLUME": "/Volumes/a/b/c"}).enabled()
    assert flock.engine_secrets("databricks") == dbx.SECRETS


def test_division_by_zero_is_spelled_out_for_databricks():
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.annotate_types import annotate_types

    from duckstring.flock.engines.databricks import _rewrite

    tree = annotate_types(sqlglot.parse_one("SELECT CAST(a AS BIGINT) / CAST(b AS BIGINT) AS q FROM t", read="duckdb"))
    sql = _rewrite(tree, exp).sql("databricks")
    assert "Infinity" in sql and "NaN" in sql and "RAISE_ERROR" not in sql.upper()
    tree = annotate_types(sqlglot.parse_one("SELECT CAST(a AS DOUBLE) / CAST(b AS DOUBLE) AS q FROM t", read="duckdb"))
    assert "RAISE_ERROR" in _rewrite(tree, exp).sql("databricks").upper()  # a DOUBLE zero may be -0.0
