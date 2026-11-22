from __future__ import annotations

import pytest
from trickle.io import first_row

from duckstring.cli import app


def _seed(root, pond: str, table: str):
    """Write an exported Parquet snapshot at ponds/{pond}/data/{table}.parquet — the read-only data
    API serves from these, exactly as a real Pond's run export would produce."""
    import duckdb

    data_dir = root / "ponds" / pond / "m1" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = str(data_dir / f"{table}.parquet").replace("'", "''")
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1 AS id, 'a' AS val UNION ALL SELECT 2, 'b') TO '{dest}' (FORMAT PARQUET)")
    con.close()


# ── get ───────────────────────────────────────────────────────────────────────


def test_get_calls_correct_endpoint(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["get", "outlet.daily"])
    assert result.exit_code == 0, result.output


def test_get_explicit_catchment(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["get", "outlet.daily", "-c", "dev"])
    assert result.exit_code == 0, result.output


def test_get_writes_to_default_path(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["get", "outlet.daily"])
    out = tmp_path / "ponds" / "outlet" / "daily"
    assert out.exists()
    assert (out / "daily.parquet").exists()


def test_get_writes_to_custom_path(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    custom = tmp_path / "my_output"
    result = runner.invoke(app, ["get", "outlet.daily", "--out", str(custom)])
    assert result.exit_code == 0, result.output
    assert (custom / "daily.parquet").exists()


def test_get_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["get", "outlet.daily", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_get_missing_ripple_exits(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["get", "ghost.nothing"])
    assert result.exit_code != 0


# ── query ─────────────────────────────────────────────────────────────────────


def test_query_with_ripple_returns_rows(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet.daily"])
    assert result.exit_code == 0, result.output
    assert "id" in result.output


def test_query_explicit_catchment(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet.daily", "-c", "dev"])
    assert result.exit_code == 0, result.output


def test_query_custom_sql(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "SELECT 42 AS answer", "--pond", "outlet"])
    assert result.exit_code == 0, result.output
    assert "42" in result.output


def test_query_sql_from_file(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    sql_file = tmp_path / "query.sql"
    sql_file.write_text('SELECT * FROM "outlet"."daily" LIMIT 1')
    result = runner.invoke(app, ["query", "@query.sql", "--pond", "outlet"])
    assert result.exit_code == 0, result.output
    assert "id" in result.output


def test_query_missing_sql_file_exits(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "@nonexistent.sql", "--pond", "outlet"])
    assert result.exit_code != 0


def test_query_prints_table(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet.daily"])
    assert result.exit_code == 0, result.output
    assert "a" in result.output
    assert "b" in result.output


def test_query_empty_result(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", 'SELECT * FROM "outlet"."daily" WHERE 1=0', "--pond", "outlet"])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_query_csv_writes_file(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "outlet.daily", "--csv", "out.csv"])
    assert result.exit_code == 0, result.output
    out = tmp_path / "ponds" / "outlet" / "daily" / "out.csv"
    assert out.exists()
    assert b"id" in out.read_bytes()


def test_query_csv_custom_path(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "outlet.daily", "--csv", "out.csv", "--path", "."])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out.csv").exists()


def test_query_json_format(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "outlet.daily", "--json", "out.json"])
    assert result.exit_code == 0, result.output
    out = tmp_path / "ponds" / "outlet" / "daily" / "out.json"
    assert out.exists()


def test_query_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["query", "outlet.daily", "-c", "nonexistent"])
    assert result.exit_code != 0


def _seed_checkpointed_merge(dd, table, n):
    """Hand-build a **checkpointed** merge Trickle layout — a large cold base (``{table}__base/``, all
    present rows stamped at ``f_base``) plus a tiny hot changelog above it (one update) plus the sidecar.
    Driving the real LSM ``checkpoint()`` in isolation is degenerate; the published *layout* is what the
    browse reconstruction reads, so we write it directly to get a genuinely large base without the machinery."""
    import json

    import duckdb

    (dd / f"{table}__base").mkdir(parents=True)
    (dd / f"{table}__changelog").mkdir()
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute(f"""COPY (SELECT range AS id, 'v'||range AS v, 1 AS _duckstring_d,
        TIMESTAMPTZ '2026-06-16T01:00:00+00:00' AS _duckstring_f FROM range({n}))
        TO '{dd}/{table}__base/2026-06-16T01_00_00+00_00__0.parquet' (FORMAT PARQUET)""")
    con.execute(f"""COPY (SELECT * FROM (VALUES
        (5, 'v5', -1, TIMESTAMPTZ '2026-06-16T02:00:00+00:00'),
        (5, 'X',   1, TIMESTAMPTZ '2026-06-16T02:00:00+00:00'))
        t(id, v, _duckstring_d, _duckstring_f))
        TO '{dd}/{table}__changelog/2026-06-16T02_00_00+00_00__0.parquet' (FORMAT PARQUET)""")
    con.close()
    (dd / "_trickle.json").write_text(json.dumps({table: {
        "mode": "merge", "pk": ["id"], "floor": "2026-06-16T01:00:00+00:00",
        "f": "2026-06-16T02:00:00+00:00", "f_base": "2026-06-16T01:00:00+00:00", "format": 2}}))


def test_merge_browse_limit_pushes_down_through_the_base(tmp_path, monkeypatch):
    """A merge main's default browse reconstructs latest-per-PK over base ⊎ changelog, but with the base
    checkpointed and the changelog clamped to ``> f_base``, a ``LIMIT`` must push down to the **base**
    Parquet scan — so browsing a huge (TB-cold) table reads a page, not the whole base. Guards against a
    regression that reintroduces a full base scan (a materialised reconstruction, a blocking sort/agg on
    the base side). Also asserts the reconstruction is still correct (the update is applied)."""
    import re

    import duckdb

    from duckstring.catchment.routes.data import _trickle_base_sql
    from duckstring.dataplane import ParquetDataPlane
    from duckstring.storage import LocalStorage

    n = 200_000
    dd = tmp_path / "d"
    _seed_checkpointed_merge(dd, "dim", n)
    dp = ParquetDataPlane()
    dds = LocalStorage(dd)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute('CREATE SCHEMA "p"')
    for t in dp.list_tables(dds):
        con.execute(f'CREATE VIEW "p"."{t}" AS {dp.read_select(dds, t)}')
    base = _trickle_base_sql("p", "dim", "merge", ["id"], None, None, f_base="2026-06-16T01:00:00+00:00")

    # Correct current state: N rows, the update applied.
    assert first_row(con.execute(f"SELECT count(*) FROM ({base})"))[0] == n
    assert first_row(con.execute(f"SELECT v FROM ({base}) WHERE id = 5"))[0] == "X"

    # Pushdown: no operator in the analysed plan touches anywhere near the whole base.
    plan = con.execute(f"EXPLAIN ANALYZE SELECT * FROM ({base}) AS _p LIMIT 5").fetchall()[0][1]
    cardinalities = [int(x.replace(",", "")) for x in re.findall(r"(\d[\d,]*)\s+[Rr]ows?", plan)]
    assert max(cardinalities) < n // 2, f"LIMIT did not push down; max operator cardinality={max(cardinalities)}"


# ── /api/query/page (the data viewer's paged read) ──────────────────────────────


def _seed_n(root, pond: str, table: str, n: int):
    """An exported Parquet snapshot of `n` rows (id 0..n-1) at ponds/{pond}/m1/data/{table}.parquet."""
    import duckdb

    data_dir = root / "ponds" / pond / "m1" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = str(data_dir / f"{table}.parquet").replace("'", "''")
    con = duckdb.connect()
    con.execute(f"COPY (SELECT i AS id, i * 10 AS v FROM range({n}) t(i)) TO '{dest}' (FORMAT PARQUET)")
    con.close()


def _serve_pond(client, name: str, tables: list[str]) -> None:
    """Register + schema-capture a pond line so the **catchment-wide serving core** (which the custom-``sql``
    branch of /query/page + /query/count now runs through) can see its published tables. A per-pond
    ``table`` browse reads the disk snapshot directly and needs none of this."""
    from duckstring.catchment.routes.deploy import _register

    db = client.app.state.db
    _register(db, name, "1.0.0", "outlet", f"ponds/{name}/1.0.0",
              {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "outlet", "serve_tables": tables},
              [{"func": "f", "name": "r", "parents": []}])
    pv = first_row(db.execute("SELECT pv.id FROM pond_version pv JOIN pond_name pn ON pn.id = pv.pond_name_id "
                    "WHERE pn.name = ? AND pv.version = '1.0.0'", (name,)))[0]
    for t in tables:
        db.execute('INSERT OR IGNORE INTO pond_version_schema (pond_version_id, "table", "column", type) '
                   "VALUES (?, ?, 'id', 'INTEGER')", (pv, t))
    db.commit()
    client.app.state.driver.reload()


def test_query_page_paginates_with_has_more(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 5)
    r = catchment_client.post("/api/query/page", json={"pond": "outlet", "table": "daily", "limit": 2, "offset": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["columns"] == ["id", "v"]
    assert body["rows"] == [[0, 0], [1, 10]]
    assert body["has_more"] is True

    # Last page: one row left, no more.
    r2 = catchment_client.post("/api/query/page", json={"pond": "outlet", "table": "daily", "limit": 2, "offset": 4})
    body2 = r2.json()
    assert body2["rows"] == [[4, 40]]
    assert body2["has_more"] is False


def test_query_page_wraps_custom_sql(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 10)
    _serve_pond(catchment_client, "outlet", ["daily"])
    # A custom query with its own LIMIT — the page wraps it as a subquery, so the user's cap still
    # bounds the result while the page reads within it.
    r = catchment_client.post(
        "/api/query/page",
        json={"pond": "outlet", "sql": 'SELECT id FROM "outlet"."daily" LIMIT 3', "limit": 2, "offset": 0},
    )
    body = r.json()
    assert body["columns"] == ["id"]
    assert body["rows"] == [[0], [1]]
    assert body["has_more"] is True  # one more within the LIMIT 3
    r2 = catchment_client.post(
        "/api/query/page",
        json={"pond": "outlet", "sql": 'SELECT id FROM "outlet"."daily" LIMIT 3', "limit": 2, "offset": 2},
    )
    assert r2.json()["rows"] == [[2]]
    assert r2.json()["has_more"] is False


def _seed_typed(root, pond, table):
    """An exported table with FLOAT/DOUBLE/DECIMAL columns for predicate-pushdown regression coverage."""
    import duckdb

    data_dir = root / "ponds" / pond / "m1" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = str(data_dir / f"{table}.parquet").replace("'", "''")
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT i AS id, (i*1.5)::FLOAT AS f_real, (i*1.5)::DOUBLE AS f_dbl, "
        f"(i*1.5)::DECIMAL(10,2) AS f_dec FROM range(20) t(i)) TO '{dest}' (FORMAT PARQUET)"
    )
    con.close()


def test_less_than_predicate_on_floats(catchment_client, tmp_path, monkeypatch):
    # Regression: a `<` predicate on FLOAT/DOUBLE/DECIMAL must return rows (not silently zero) through
    # both /query/count and the wrapped /query/page — and agree with each other.
    _seed_typed(tmp_path, "p", "t")
    _serve_pond(catchment_client, "p", ["t"])
    for col in ("f_real", "f_dbl", "f_dec"):
        sql = f'SELECT * FROM "p"."t" WHERE {col} < 5 LIMIT 1000'
        count = catchment_client.post("/api/query/count", json={"pond": "p", "sql": sql}).json()["count"]
        page = catchment_client.post("/api/query/page", json={"pond": "p", "sql": sql, "limit": 500}).json()
        assert count == 4, f"{col} < 5 count"
        assert len(page["rows"]) == 4, f"{col} < 5 page"


def test_query_page_bad_sql_is_400(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 1)
    r = catchment_client.post("/api/query/page", json={"pond": "outlet", "sql": "SELECT nope FROM missing"})
    assert r.status_code == 400


def test_query_page_order_by_column(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 5)  # rows id 0..4 in ascending scan order
    page = catchment_client.post(
        "/api/query/page", json={"pond": "outlet", "table": "daily", "order_by": "id", "order_desc": True, "limit": 100}
    ).json()
    idx = {c: i for i, c in enumerate(page["columns"])}
    assert [r[idx["id"]] for r in page["rows"]] == [4, 3, 2, 1, 0]


def test_query_page_order_by_unknown_column_400(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 2)
    r = catchment_client.post("/api/query/page", json={"pond": "outlet", "table": "daily", "order_by": "nope"})
    assert r.status_code == 400


def test_list_pond_tables(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 1)
    _seed_n(tmp_path, "outlet", "hourly", 1)
    r = catchment_client.get("/api/ponds/outlet/tables")
    assert r.status_code == 200
    tables = r.json()["tables"]
    assert sorted(t["name"] for t in tables) == ["daily", "hourly"]
    # Plain (non-Trickle) tables carry no trickle mode / pk.
    assert all(t["trickle"] is None and t["pk"] == [] for t in tables)


def test_list_pond_tables_empty(catchment_client, tmp_path):
    # An unknown pond with no exported data resolves to no tables (no error).
    (tmp_path / "ponds" / "ghost" / "m1" / "data").mkdir(parents=True)
    assert catchment_client.get("/api/ponds/ghost/tables").json()["tables"] == []


def test_query_count_table_and_sql(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 7)
    _serve_pond(catchment_client, "outlet", ["daily"])
    assert catchment_client.post("/api/query/count", json={"pond": "outlet", "table": "daily"}).json()["count"] == 7
    # A custom query's count reflects its own shape (here a LIMIT). Custom sql runs through the serving core.
    r = catchment_client.post(
        "/api/query/count", json={"pond": "outlet", "sql": 'SELECT * FROM "outlet"."daily" LIMIT 3'}
    )
    assert r.json()["count"] == 3


def test_query_count_bad_sql_is_400(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 1)
    r = catchment_client.post("/api/query/count", json={"pond": "outlet", "sql": "SELECT * FROM missing"})
    assert r.status_code == 400


# ── Trickle browse (merge consolidation + freshness window + history) ───────────


def _seed_merge_trickle(root, pond, table, pk, runs):
    """Build a real merge Trickle (main + Z-set changelog + sidecar) by replaying `runs`, each a
    ``(f_iso, [(id, name, price), ...])`` complete-state snapshot, then export the flat Parquet."""
    from datetime import datetime

    import duckdb

    from duckstring import trickle_io
    from duckstring.dataplane import ParquetDataPlane

    data_dir = root / "ponds" / pond / "m1" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    for f_iso, rows in runs:
        con.execute("CREATE OR REPLACE TEMP TABLE _state(id BIGINT, name VARCHAR, price DOUBLE)")
        con.executemany("INSERT INTO _state VALUES (?, ?, ?)", rows)
        trickle_io.merge_table(con, table, con.sql("SELECT * FROM _state"), datetime.fromisoformat(f_iso), pk)
    ParquetDataPlane().export(con, data_dir)
    con.close()


# id1 updated, id2 deleted, id3 added at f2; id4 untouched since the f1 bootstrap.
_MERGE_RUNS = [
    ("2026-01-01T00:00:00+00:00", [(1, "a", 10.0), (2, "b", 20.0), (4, "d", 40.0)]),
    ("2026-01-02T00:00:00+00:00", [(1, "a", 15.0), (3, "c", 30.0), (4, "d", 40.0)]),
]


def test_tables_flags_trickle_mode_and_pk(catchment_client, tmp_path, monkeypatch):
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    by = {t["name"]: t for t in catchment_client.get("/api/ponds/p/tables").json()["tables"]}
    assert by["priced"]["trickle"] == "merge" and by["priced"]["pk"] == ["id"]
    # The changelog companion stays a plain table — raw-navigable, unchanged.
    assert by["priced__changelog"]["trickle"] is None


def test_freshness_lists_incremental_runs(catchment_client, tmp_path, monkeypatch):
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    fr = catchment_client.get("/api/ponds/p/tables/priced/runs").json()
    # The main is log-structured, so the bootstrap (f1) writes the changelog too — both runs show
    # (newest first); floor anchors at f1.
    assert len(fr["freshness"]) == 2
    assert fr["freshness"][0].startswith("2026-01-02")
    assert fr["freshness"][1].startswith("2026-01-01")
    assert fr["floor"].startswith("2026-01-01")


def _rows_by_id(page):
    idx = {c: i for i, c in enumerate(page["columns"])}
    return idx, {r[idx["id"]]: r for r in page["rows"]}


def _view(page):
    idx = {c: i for i, c in enumerate(page["columns"])}
    return [(r[idx["id"]], r[idx["price"]], r[idx["_duckstring_active"]]) for r in page["rows"]], idx


def test_merge_default_view_is_its_log_newest_first(catchment_client, tmp_path, monkeypatch):
    # The images written since the base come first (newest first), then the base, each marked current (1),
    # replaced by a later write (0) or deleted (-1). Nothing consolidates the change log.
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    page = catchment_client.post(
        "/api/query/page", json={"pond": "p", "table": "priced", "trickle": "merge", "pk": ["id"], "limit": 100}
    ).json()
    rows, idx = _view(page)
    assert "_duckstring_updates" not in idx and "_duckstring_f" in idx
    assert sorted(rows[:2]) == [(1, 15.0, 1), (3, 30.0, 1)]  # the f2 images
    assert sorted(rows[2:]) == [(1, 10.0, 0), (2, 20.0, -1), (4, 40.0, 1)]  # the f1 bootstrap, in the base
    by_id = {r[idx["id"]]: r for r in page["rows"][2:]}
    assert by_id[4][idx["_duckstring_f"]].startswith("2026-01-01")  # a base row keeps its own write time
    assert page["rows"][0][idx["_duckstring_f"]].startswith("2026-01-02")


def test_merge_log_pages_across_its_regions(catchment_client, tmp_path, monkeypatch):
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    body = {"pond": "p", "table": "priced", "trickle": "merge", "pk": ["id"]}
    whole, _ = _view(catchment_client.post("/api/query/page", json={**body, "limit": 100}).json())
    pages = []
    for offset in range(0, 6, 2):
        got = catchment_client.post("/api/query/page", json={**body, "limit": 2, "offset": offset}).json()
        pages += _view(got)[0]
        assert got["has_more"] == (offset + 2 < 5)
    assert pages == whole


def test_merge_order_by_pk_is_opt_in(catchment_client, tmp_path, monkeypatch):
    # A sort orders the whole log (only then is the full-scan sort paid); each row keeps its status.
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    page = catchment_client.post(
        "/api/query/page",
        json={"pond": "p", "table": "priced", "trickle": "merge", "pk": ["id"], "order_by": "id", "limit": 100},
    ).json()
    rows, _ = _view(page)
    assert [r[0] for r in rows] == [1, 1, 2, 3, 4]
    assert sorted(rows) == [(1, 10.0, 0), (1, 15.0, 1), (2, 20.0, -1), (3, 30.0, 1), (4, 40.0, 1)]


def test_merge_order_by_freshness(catchment_client, tmp_path, monkeypatch):
    # Opt-in: the consolidated view can be re-sorted by any column, including freshness.
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    page = catchment_client.post(
        "/api/query/page",
        json={"pond": "p", "table": "priced", "trickle": "merge", "pk": ["id"],
              "order_by": "_duckstring_f", "order_desc": True, "limit": 100},
    ).json()
    idx = {c: i for i, c in enumerate(page["columns"])}
    fs = [r[idx["_duckstring_f"]] for r in page["rows"]]
    assert fs == sorted(fs, reverse=True)
    assert page["rows"][-1][idx["id"]] == 4  # id4 at the floor freshness sorts last


def test_merge_window_shows_only_changed_records(catchment_client, tmp_path, monkeypatch):
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    f2 = catchment_client.get("/api/ponds/p/tables/priced/runs").json()["freshness"][0]
    page = catchment_client.post(
        "/api/query/page",
        json={"pond": "p", "table": "priced", "trickle": "merge", "pk": ["id"], "f_lo": f2, "f_hi": f2, "limit": 100},
    ).json()
    _, by = _rows_by_id(page)
    assert set(by) == {1, 2, 3}  # id4 untouched in this run → excluded


def test_merge_count_matches(catchment_client, tmp_path, monkeypatch):
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    n = catchment_client.post(
        "/api/query/count", json={"pond": "p", "table": "priced", "trickle": "merge", "pk": ["id"]}
    ).json()["count"]
    assert n == 5  # the two images since the base, and the base's three rows


def test_merge_count_fast_path_matches_checkpointed(catchment_client, tmp_path, monkeypatch):
    """The fast count (base metadata + changelog net weight + tombstones) must equal the page's total even
    once the main is checkpointed (a real f_base, with retained pre-base changelog history)."""
    from datetime import datetime

    import duckdb

    from duckstring import trickle_io
    from duckstring.dataplane import ParquetDataPlane

    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1024")  # tiny → force a checkpoint (real base + f_base)
    data_dir = tmp_path / "ponds" / "p" / "m1" / "data"
    data_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    dp = ParquetDataPlane()
    runs = [
        ("2026-01-01T00:00:00+00:00", [(1, 10), (2, 20), (3, 30), (4, 40)]),
        ("2026-01-02T00:00:00+00:00", [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)]),  # +insert 5 → checkpoint
        ("2026-01-03T00:00:00+00:00", [(1, 11), (2, 20), (3, 30), (4, 40), (5, 50)]),  # update 1
        ("2026-01-04T00:00:00+00:00", [(1, 11), (3, 30), (4, 40), (5, 50)]),           # delete 2
    ]
    for f_iso, rows in runs:
        con.execute("CREATE OR REPLACE TEMP TABLE _s(id BIGINT, v BIGINT)")
        con.executemany("INSERT INTO _s VALUES (?, ?)", rows)
        f = datetime.fromisoformat(f_iso)
        trickle_io.merge_table(con, "t", con.sql("SELECT * FROM _s"), f, ("id",), retain_n=10**9)
        dp.export(con, data_dir, f=f)
    con.close()
    assert trickle_io.load_sidecar(data_dir).get("t", {}).get("f_base")  # actually checkpointed

    body = {"pond": "p", "table": "t", "trickle": "merge", "pk": ["id"]}
    count = catchment_client.post("/api/query/count", json=body).json()["count"]
    page = catchment_client.post("/api/query/page", json={**body, "limit": 1000}).json()
    idx = {c: i for i, c in enumerate(page["columns"])}
    # The fast count (base metadata + changelog net weight + tombstones) must equal the rows the page shows.
    assert count == len(page["rows"])
    active = {r[idx["id"]] for r in page["rows"] if r[idx["_duckstring_active"]] > 0}
    assert active == {1, 3, 4, 5}  # current state (id2 deleted)


def _seed_append_trickle(root, pond, table, runs):
    """Build an append Trickle (insert-only history + sidecar) by appending each run's rows."""
    from datetime import datetime

    import duckdb

    from duckstring import trickle_io
    from duckstring.dataplane import ParquetDataPlane

    data_dir = root / "ponds" / pond / "m1" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    for f_iso, rows in runs:
        con.execute("CREATE OR REPLACE TEMP TABLE _e(id BIGINT, amt DOUBLE)")
        con.executemany("INSERT INTO _e VALUES (?, ?)", rows)
        f = datetime.fromisoformat(f_iso)
        trickle_io.append_table(con, table, con.sql("SELECT * FROM _e"), f, ("id",))
        ParquetDataPlane().export(con, data_dir, f=f)  # one part per Run, as a Duck publishes
    con.close()


def test_append_default_view_is_newest_run_first(catchment_client, tmp_path, monkeypatch):
    _seed_append_trickle(tmp_path, "p", "events", [
        ("2026-01-01T00:00:00+00:00", [(1, 1.0), (2, 2.0)]),
        ("2026-01-02T00:00:00+00:00", [(3, 3.0)]),
        ("2026-01-03T00:00:00+00:00", [(4, 4.0), (5, 5.0)]),
    ])
    body = {"pond": "p", "table": "events", "trickle": "append", "pk": ["id"]}
    page = catchment_client.post("/api/query/page", json={**body, "limit": 100}).json()
    idx = {c: i for i, c in enumerate(page["columns"])}
    assert [r[idx["id"]] for r in page["rows"]] == [4, 5, 3, 1, 2]
    assert catchment_client.post("/api/query/count", json=body).json()["count"] == 5
    second = catchment_client.post("/api/query/page", json={**body, "limit": 2, "offset": 2}).json()
    assert [r[idx["id"]] for r in second["rows"]] == [3, 1]


def test_append_window_filters_by_freshness(catchment_client, tmp_path, monkeypatch):
    _seed_append_trickle(tmp_path, "p", "orders", [
        ("2026-01-01T00:00:00+00:00", [(1, 1.0), (2, 2.0)]),
        ("2026-01-02T00:00:00+00:00", [(3, 3.0)]),
    ])
    by = {t["name"]: t for t in catchment_client.get("/api/ponds/p/tables").json()["tables"]}
    assert by["orders"]["trickle"] == "append"
    # Append history records every run's freshness (no bootstrap exclusion).
    fr = catchment_client.get("/api/ponds/p/tables/orders/runs").json()["freshness"]
    assert len(fr) == 2
    # Full history = 3 rows; windowed to the latest run = 1 row.
    full = catchment_client.post(
        "/api/query/count", json={"pond": "p", "table": "orders", "trickle": "append", "pk": ["id"]}
    ).json()["count"]
    assert full == 3
    f2 = fr[0]
    win = catchment_client.post(
        "/api/query/count",
        json={"pond": "p", "table": "orders", "trickle": "append", "pk": ["id"], "f_lo": f2, "f_hi": f2},
    ).json()["count"]
    assert win == 1


def test_merge_row_history(catchment_client, tmp_path, monkeypatch):
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    # id1 was bootstrap-created (10.0) then updated at f2 (15.0). The update is shown, and the original
    # bootstrap image is surfaced as a synthetic 'create' at the bottom (from the oldest -1).
    hist = catchment_client.post(
        "/api/query/history", json={"pond": "p", "table": "priced", "pk": {"id": 1}}
    ).json()
    idx = {c: i for i, c in enumerate(hist["columns"])}
    assert "_duckstring_d" not in hist["columns"]  # collapsed to an event label, not raw weights
    assert [r[idx["_duckstring_event"]] for r in hist["rows"]] == ["update", "create"]
    assert hist["rows"][0][idx["price"]] == 15.0  # the surviving (+1) image
    assert hist["rows"][-1][idx["price"]] == 10.0  # the recovered original (bottom)
    assert hist["rows"][-1][idx["_duckstring_f"]].startswith("2026-01-01")  # at the floor freshness


def test_merge_history_create_and_delete_events(catchment_client, tmp_path, monkeypatch):
    _seed_merge_trickle(tmp_path, "p", "priced", ("id",), _MERGE_RUNS)
    idx_of = lambda h: {c: i for i, c in enumerate(h["columns"])}  # noqa: E731
    # id3 was inserted at f2 → 'create'.
    h3 = catchment_client.post("/api/query/history", json={"pond": "p", "table": "priced", "pk": {"id": 3}}).json()
    assert [r[idx_of(h3)["_duckstring_event"]] for r in h3["rows"]] == ["create"]
    # id2 was created at bootstrap (f1) then deleted at f2 → 'delete' over 'create' (newest first), the
    # delete showing the retracted image.
    h2 = catchment_client.post("/api/query/history", json={"pond": "p", "table": "priced", "pk": {"id": 2}}).json()
    i2 = idx_of(h2)
    assert [r[i2["_duckstring_event"]] for r in h2["rows"]] == ["delete", "create"]
    assert h2["rows"][0][i2["name"]] == "b"


@pytest.mark.timeout(60)
def test_merge_log_view_agrees_with_the_current_state_through_folds_and_compactions(catchment_client, tmp_path,
                                                                                     monkeypatch):
    """Random updates, deletes and re-adds, with a tiny chunk threshold so the main folds bands and compacts its
    base: the log view's current rows are exactly the reconstructed state, each change image appears once, and the
    count is the rows it pages through."""
    import random
    from datetime import datetime, timedelta, timezone

    import duckdb

    from duckstring import trickle_io
    from duckstring.dataplane import ParquetDataPlane

    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1024")  # with a 2,000-row base: bands, then a hot tail
    data_dir = tmp_path / "ponds" / "p" / "m1" / "data"
    data_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    dp, rng = ParquetDataPlane(), random.Random(7)
    state = {i: rng.randint(0, 9) for i in range(2000)}
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for run in range(40):
        for k in rng.sample(range(2020), 12):
            if k in state and rng.random() < 0.3:
                del state[k]
            else:
                state[k] = rng.randint(0, 9)
        con.execute("CREATE OR REPLACE TEMP TABLE _s(id BIGINT, v BIGINT)")
        con.executemany("INSERT INTO _s VALUES (?, ?)", sorted(state.items()))
        f = t0 + timedelta(hours=run)
        trickle_io.merge_table(con, "t", con.sql("SELECT * FROM _s"), f, ("id",))
        dp.export(con, data_dir, f=f)
    con.close()

    body = {"pond": "p", "table": "t", "trickle": "merge", "pk": ["id"]}
    count = catchment_client.post("/api/query/count", json=body).json()["count"]
    rows = []
    for offset in range(0, count + 500, 500):
        page = catchment_client.post("/api/query/page", json={**body, "limit": 500, "offset": offset}).json()
        idx = {c: i for i, c in enumerate(page["columns"])}
        rows += [(r[idx["id"]], r[idx["v"]], r[idx["_duckstring_f"]], r[idx["_duckstring_active"]]) for r in page["rows"]]
    assert len(rows) == count
    assert len(set(rows)) == len(rows)  # no image shown twice (a hot union overlapping a band, say)
    assert sorted((k, v) for k, v, _, a in rows if a == 1) == sorted(state.items())
    gone = {k for k, _, _, a in rows if a == -1}
    assert gone and not gone & set(state)
    assert (data_dir / "t__band").is_dir() and any((data_dir / "t__changelog").iterdir())  # every region
    assert rows[0][2] == max(r[2] for r in rows)  # newest first


def test_pages_are_placed_by_file_and_read_only_the_files_they_fall_in(catchment_client, tmp_path, monkeypatch):
    """A page is placed from each file's row count, so a page deep in a large table reads only its own file or two.
    Paged in odd steps across many files, with and without a freshness window, it gives the rows the whole read
    gives, in the same order."""
    runs = [(f"2026-01-01T{h:02d}:00:00+00:00", [(h * 100 + i, float(i)) for i in range(h % 4 + 1)]) for h in range(12)]
    _seed_append_trickle(tmp_path, "p", "events", runs)
    for window in ({}, {"f_lo": "2026-01-01T03:00:00+00:00", "f_hi": "2026-01-01T08:00:00+00:00"}):
        body = {"pond": "p", "table": "events", "trickle": "append", "pk": ["id"], **window}
        whole = catchment_client.post("/api/query/page", json={**body, "limit": 1000}).json()
        idx = whole["columns"].index("id")
        ids = [r[idx] for r in whole["rows"]]
        hours = [i // 100 for i in ids]
        assert hours == sorted(hours, reverse=True)
        expected = [h for h in range(12) if not window or 3 <= h <= 8]
        assert sorted(set(hours), reverse=True) == sorted(expected, reverse=True)
        assert catchment_client.post("/api/query/count", json=body).json()["count"] == len(ids)
        paged = []
        for offset in range(0, len(ids) + 3, 3):
            page = catchment_client.post("/api/query/page", json={**body, "limit": 3, "offset": offset}).json()
            paged += [r[idx] for r in page["rows"]]
        assert paged == ids


def _seed_reports(root, pond, rows_by_run):
    """Publish a Pond's Run reports as the export does: per Run ``f``, rows ``(pond_key, run_f, {reports})``."""
    import json
    from datetime import datetime

    import duckdb

    from duckstring import trickle_io
    from duckstring.dataplane import ParquetDataPlane
    from duckstring.reports import PK, REPORTS_TABLE

    data_dir = root / "ponds" / pond / "m1" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    for f_iso, rows in rows_by_run:
        f = datetime.fromisoformat(f_iso)
        con.execute("CREATE OR REPLACE TEMP TABLE _r(pond VARCHAR, run_f TIMESTAMPTZ, reports VARCHAR)")
        con.executemany("INSERT INTO _r VALUES (?, ?, ?)", [(k, datetime.fromisoformat(rf), json.dumps(r))
                                                            for k, rf, r in rows])
        trickle_io.append_table(con, REPORTS_TABLE, con.sql("SELECT * FROM _r"), f, PK, on_conflict="keep_prior")
        ParquetDataPlane().export(con, data_dir, f=f)
    con.close()


def test_a_ponds_run_reports_are_served(catchment_client, tmp_path, monkeypatch):
    """pond.report() facts were readable only by querying a hidden table by name. The reports route lists a Pond's
    own reports, newest first, and gives one Run's own report with the upstream ones it read."""
    t1, t2 = "2026-01-01T01:00:00+00:00", "2026-01-01T02:00:00+00:00"
    _seed_reports(tmp_path, "p", [
        (t1, [("p@1", t1, {"rows": 10}), ("src@1", "2026-01-01T00:30:00+00:00", {"newest_hour": "00:00"})]),
        (t2, [("p@1", t2, {"rows": 12}), ("src@1", "2026-01-01T01:30:00+00:00", {"newest_hour": "01:00"}),
              ("src@1", "2026-01-01T01:45:00+00:00", {"newest_hour": "01:00"})]),
    ])
    out = catchment_client.get("/api/ponds/p/reports").json()
    assert [(r["f"][:19], r["reports"]) for r in out["reports"]] == [
        ("2026-01-01T02:00:00", {"rows": 12}), ("2026-01-01T01:00:00", {"rows": 10})]
    run = catchment_client.get("/api/ponds/p/reports", params={"f": t2}).json()
    assert run["reports"] == {"rows": 12}
    assert [(u["pond"], u["f"][:19], u["reports"]) for u in run["upstream"]] == [
        ("src@1", "2026-01-01T01:30:00", {"newest_hour": "01:00"}),
        ("src@1", "2026-01-01T01:45:00", {"newest_hour": "01:00"})]
    assert catchment_client.get("/api/ponds/p/reports", params={"limit": 1}).json()["reports"][0]["reports"] == {"rows": 12}
    # A Pond that never reported: nothing, not an error.
    _seed_append_trickle(tmp_path, "q", "events", [("2026-01-01T00:00:00+00:00", [(1, 1.0)])])
    assert catchment_client.get("/api/ponds/q/reports").json() == {"reports": []}
    assert catchment_client.get("/api/ponds/q/reports", params={"f": t1}).json() == {
        "f": t1, "reports": None, "upstream": []}
