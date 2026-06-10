from __future__ import annotations

from duckstring.cli import app


def _seed(root, pond: str, table: str):
    """Write an exported Parquet snapshot at ponds/{pond}/data/{table}.parquet — the read-only data
    API serves from these, exactly as a real Pond's run export would produce."""
    import duckdb

    data_dir = root / "ponds" / pond / "data"
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


def test_query_custom_sql(runner, live_catchment):
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
