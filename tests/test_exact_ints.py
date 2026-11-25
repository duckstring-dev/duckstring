"""Integer sums published exactly (plans/showcase-fixes.md item 9). DuckDB's ``SUM`` over integers is ``HUGEINT``, which
Parquet can't hold, so it was published as ``DOUBLE``: a count of 9 read as 9.0 downstream, and anything above
2^53 was rounded, including an aggregate's state once a Duck rebuilt its registry from what it had published.
A line first deployed from 0.9 on publishes ``BIGINT``; older lines keep ``DOUBLE``, which their Sinks' contracts
record. An aggregate's state is always published exactly."""

from __future__ import annotations

import glob
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
import trickle
from trickle import agg

from duckstring.dataplane import ParquetDataPlane, hydrate_registry, stage_boot_base
from duckstring.schema_contract import extract_schema
from duckstring.storage import LocalStorage

F1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
F2 = F1 + timedelta(hours=1)
BIG = 9007199254740993  # 2^53 + 1: the first integer DOUBLE can't hold


def _types(paths: list[str]) -> dict[str, str]:
    rel = duckdb.sql(f"SELECT * FROM read_parquet({paths}, union_by_name=true)")
    return {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}


def _registry(tmp_path):
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    con.execute("CREATE TABLE ev_src AS SELECT range % 3 AS g, range::BIGINT AS b FROM range(9)")
    ep = trickle.at(con, F1)
    ep.append_table("ev", con.sql("SELECT g, b FROM ev_src"))
    trickle.at(con, F1).source("ev").aggregate(by="g", s=agg.sum("b"), n=agg.count()).merge("per_g")
    con.execute("CREATE TABLE plain AS SELECT g, sum(b) AS total FROM ev_src GROUP BY g")
    return con


@pytest.mark.parametrize("exact, want", [(True, "BIGINT"), (False, "DOUBLE")])
def test_every_publish_path_follows_the_lines_flag(tmp_path, exact, want):
    con = _registry(tmp_path)
    data = tmp_path / "data"
    ParquetDataPlane().export(con, data, f=F1, exact_ints=exact)
    # A second Run: a change log part for the aggregate.
    trickle.at(con, F2, F1).append_table("ev", con.sql("SELECT 1 AS g, 100::BIGINT AS b"))
    trickle.at(con, F2, F1).source("ev").aggregate(by="g", s=agg.sum("b"), n=agg.count()).merge("per_g")
    ParquetDataPlane().export(con, data, f=F2, exact_ints=exact)
    assert _types(glob.glob(f"{data}/plain__v/*.parquet"))["total"] == want
    assert _types(glob.glob(f"{data}/per_g__base/*.parquet"))["s"] == want
    assert _types(glob.glob(f"{data}/per_g__changelog/*.parquet"))["s"] == want
    assert _types(glob.glob(f"{data}/per_g__base/*.parquet"))["n"] == "BIGINT"  # a count was always BIGINT
    assert extract_schema(con, exact)["plain"]["total"] == want  # what the contract records


def test_a_merge_tables_first_load_is_staged_exactly(tmp_path):
    con = duckdb.connect()
    store = LocalStorage(tmp_path / "data")
    sql = stage_boot_base(con, store, "t", "SELECT 1 AS k, sum(range)::HUGEINT AS s FROM range(4)", exact_ints=True)
    assert _types(glob.glob(f"{tmp_path}/data/.boot/t/*.parquet"))["s"] == "BIGINT" and sql


def test_a_sum_too_large_for_bigint_fails_the_publish_naming_the_column(tmp_path):
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    con.execute("CREATE TABLE plain AS SELECT (9223372036854775807::HUGEINT * 4) AS total")
    with pytest.raises(duckdb.Error, match="'plain.total' holds a value outside BIGINT's range"):
        ParquetDataPlane().export(con, tmp_path / "data", f=F1, exact_ints=True)


def test_an_aggregates_state_survives_a_rebuilt_registry_exactly(tmp_path):
    """The repro: a Duck rebuilt its registry from published state and summed 12 + (2^53 + 1) as
    9007199254741004.0."""
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    data = LocalStorage(tmp_path / "data")
    con.execute("CREATE TABLE src AS SELECT range % 3 AS g, range::BIGINT AS b FROM range(9)")
    trickle.at(con, F1).append_table("ev", con.sql("SELECT g, b FROM src"))
    trickle.at(con, F1).source("ev").aggregate(by="g", s=agg.sum("b")).merge("agg")
    ParquetDataPlane().export(con, data, f=F1, exact_ints=True)
    con.close()

    fresh = duckdb.connect(str(tmp_path / "r2.duckdb"))
    hydrate_registry(fresh, data)
    state = {c: t for c, t, *_ in fresh.sql("DESCRIBE _duckstring_agg_agg").fetchall()}
    assert state["_a_sum_0"] == "HUGEINT"
    trickle.at(fresh, F2, F1).append_table("ev", fresh.sql(f"SELECT 1::BIGINT AS g, {BIG}::BIGINT AS b"))
    trickle.at(fresh, F2, F1).source("ev").aggregate(by="g", s=agg.sum("b")).merge("agg")
    assert dict(fresh.sql("SELECT g, s FROM agg").fetchall())[1] == 1 + 4 + 7 + BIG
    sums = dict(fresh.sql("SELECT g, _a_sum_0 FROM _duckstring_agg_agg").fetchall())
    assert sums[1] == 1 + 4 + 7 + BIG  # the state itself is exact, whatever the line publishes


def test_an_older_lines_state_is_exact_too(tmp_path):
    con = duckdb.connect(str(tmp_path / "r.duckdb"))
    data = LocalStorage(tmp_path / "data")
    trickle.at(con, F1).append_table("ev", con.sql(f"SELECT 1::BIGINT AS g, {BIG}::BIGINT AS b"))
    trickle.at(con, F1).source("ev").aggregate(by="g", s=agg.sum("b")).merge("agg")
    ParquetDataPlane().export(con, data, f=F1)  # exact_ints off: the output is DOUBLE, its state isn't
    fresh = duckdb.connect(str(tmp_path / "r2.duckdb"))
    hydrate_registry(fresh, data)
    assert fresh.sql("SELECT _a_sum_0 FROM _duckstring_agg_agg").fetchall() == [(BIG,)]


def test_a_new_line_publishes_exactly_and_an_existing_one_keeps_double(catchment_client):
    client = catchment_client
    driver = client.app.state.driver
    _deploy_outlet_via(client)
    assert driver.meta["outlet@1"]["exact_ints"] is True
    driver.refresh_now("outlet@1")
    begin = [j for j in driver.jobs.get("outlet@1", []) if j.get("kind") == "begin_run"]
    assert begin and begin[-1]["exact_ints"] is True  # the Duck learns it from the job
    with driver.lock:  # a line that existed before migration 037
        driver.db.execute("UPDATE pond SET exact_ints = 0")
        driver.db.commit()
    driver.reload()
    assert driver.meta["outlet@1"]["exact_ints"] is False
    _deploy_outlet_via(client, version="1.0.1")  # a redeploy doesn't change the line's flag
    assert driver.meta["outlet@1"]["exact_ints"] is False


def _deploy_outlet_via(client, version: str = "1.0.0") -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", f'[pond]\nname = "outlet"\nversion = "{version}"\ntype = "outlet"\n')
    r = client.post("/api/deploy", files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
                    data={"name": "outlet", "version": version, "type": "outlet"})
    assert r.status_code == 200, r.text
