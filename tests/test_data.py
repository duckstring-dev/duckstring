from __future__ import annotations

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
    result = runner.invoke(app, ["get", "outlet", "daily"])
    assert result.exit_code == 0, result.output


def test_get_explicit_catchment(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["get", "outlet", "daily", "-c", "dev"])
    assert result.exit_code == 0, result.output


def test_get_writes_to_default_path(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["get", "outlet", "daily"])
    out = tmp_path / "ponds" / "outlet" / "daily"
    assert out.exists()
    assert (out / "daily.parquet").exists()


def test_get_writes_to_custom_path(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    custom = tmp_path / "my_output"
    result = runner.invoke(app, ["get", "outlet", "daily", "--path", str(custom)])
    assert result.exit_code == 0, result.output
    assert (custom / "daily.parquet").exists()


def test_get_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["get", "outlet", "daily", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_get_missing_ripple_exits(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["get", "ghost", "nothing"])
    assert result.exit_code != 0


# ── query ─────────────────────────────────────────────────────────────────────


def test_query_with_ripple_returns_rows(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet", "daily"])
    assert result.exit_code == 0, result.output
    assert "id" in result.output


def test_query_explicit_catchment(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet", "daily", "-c", "dev"])
    assert result.exit_code == 0, result.output


def test_query_custom_sql(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet", "--sql", "SELECT 42 AS answer"])
    assert result.exit_code == 0, result.output
    assert "42" in result.output


def test_query_sql_from_file(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    sql_file = tmp_path / "query.sql"
    sql_file.write_text('SELECT * FROM "outlet"."daily" LIMIT 1')
    result = runner.invoke(app, ["query", "outlet", "--sql", "@query.sql"])
    assert result.exit_code == 0, result.output
    assert "id" in result.output


def test_query_missing_sql_file_exits(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "outlet", "--sql", "@nonexistent.sql"])
    assert result.exit_code != 0


def test_query_prints_table(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet", "daily"])
    assert result.exit_code == 0, result.output
    assert "a" in result.output
    assert "b" in result.output


def test_query_empty_result(runner, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    result = runner.invoke(app, ["query", "outlet", "--sql", 'SELECT * FROM "outlet"."daily" WHERE 1=0'])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_query_csv_writes_file(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "outlet", "daily", "--csv", "out.csv"])
    assert result.exit_code == 0, result.output
    out = tmp_path / "ponds" / "outlet" / "daily" / "out.csv"
    assert out.exists()
    assert b"id" in out.read_bytes()


def test_query_csv_custom_path(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "outlet", "daily", "--csv", "out.csv", "--path", "."])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out.csv").exists()


def test_query_json_format(runner, tmp_path, monkeypatch, catchment_root, live_catchment):
    _seed(catchment_root, "outlet", "daily")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "outlet", "daily", "--json", "out.json"])
    assert result.exit_code == 0, result.output
    out = tmp_path / "ponds" / "outlet" / "daily" / "out.json"
    assert out.exists()


def test_query_unknown_catchment_exits(runner):
    result = runner.invoke(app, ["query", "outlet", "-c", "nonexistent"])
    assert result.exit_code != 0


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


def test_query_page_bad_sql_is_400(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 1)
    r = catchment_client.post("/api/query/page", json={"pond": "outlet", "sql": "SELECT nope FROM missing"})
    assert r.status_code == 400


def test_list_pond_tables(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 1)
    _seed_n(tmp_path, "outlet", "hourly", 1)
    r = catchment_client.get("/api/ponds/outlet/tables")
    assert r.status_code == 200
    assert sorted(r.json()["tables"]) == ["daily", "hourly"]


def test_list_pond_tables_empty(catchment_client, tmp_path):
    # An unknown pond with no exported data resolves to no tables (no error).
    (tmp_path / "ponds" / "ghost" / "m1" / "data").mkdir(parents=True)
    assert catchment_client.get("/api/ponds/ghost/tables").json()["tables"] == []


def test_query_count_table_and_sql(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 7)
    assert catchment_client.post("/api/query/count", json={"pond": "outlet", "table": "daily"}).json()["count"] == 7
    # A custom query's count reflects its own shape (here a LIMIT).
    r = catchment_client.post(
        "/api/query/count", json={"pond": "outlet", "sql": 'SELECT * FROM "outlet"."daily" LIMIT 3'}
    )
    assert r.json()["count"] == 3


def test_query_count_bad_sql_is_400(catchment_client, tmp_path):
    _seed_n(tmp_path, "outlet", "daily", 1)
    r = catchment_client.post("/api/query/count", json={"pond": "outlet", "sql": "SELECT * FROM missing"})
    assert r.status_code == 400
