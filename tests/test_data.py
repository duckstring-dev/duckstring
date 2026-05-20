from __future__ import annotations

import io
import json
import zipfile

from duckstring.cli import app


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


# ── get ───────────────────────────────────────────────────────────────────────


def test_get_calls_correct_endpoint(runner, tmp_path, monkeypatch, dev_catchment, mock_get):
    monkeypatch.chdir(tmp_path)
    mock_get.return_value.content = _make_zip({"daily.parquet": "data"})
    result = runner.invoke(app, ["get", "dev", "outlet", "daily"])
    assert result.exit_code == 0
    mock_get.assert_called_once()
    assert "/api/ponds/outlet/ripples/daily" in mock_get.call_args.args[0]


def test_get_writes_to_default_path(runner, tmp_path, monkeypatch, dev_catchment, mock_get):
    monkeypatch.chdir(tmp_path)
    mock_get.return_value.content = _make_zip({"daily.parquet": "parquet-bytes"})
    runner.invoke(app, ["get", "dev", "outlet", "daily"])
    out = tmp_path / "ponds" / "outlet" / "daily"
    assert out.exists()
    assert (out / "daily.parquet").read_text() == "parquet-bytes"


def test_get_writes_to_custom_path(runner, tmp_path, monkeypatch, dev_catchment, mock_get):
    monkeypatch.chdir(tmp_path)
    custom = tmp_path / "my_output"
    mock_get.return_value.content = _make_zip({"result.parquet": "data"})
    result = runner.invoke(app, ["get", "dev", "outlet", "daily", "--path", str(custom)])
    assert result.exit_code == 0
    assert (custom / "result.parquet").exists()


def test_get_unknown_catchment_exits(runner, mock_get):
    result = runner.invoke(app, ["get", "nonexistent", "outlet", "daily"])
    assert result.exit_code != 0
    assert mock_get.call_count == 0


# ── query ─────────────────────────────────────────────────────────────────────


def _json_response(mock, rows):
    mock.return_value.json.return_value = rows
    mock.return_value.content = json.dumps(rows).encode()


def test_query_with_ripple_sends_default_sql(runner, dev_catchment, mock_post):
    _json_response(mock_post, [{"id": 1, "value": "x"}])
    result = runner.invoke(app, ["query", "dev", "outlet", "daily"])
    assert result.exit_code == 0
    payload = mock_post.call_args.kwargs["json"]
    assert "SELECT" in payload["sql"]
    assert "outlet.daily" in payload["sql"]


def test_query_custom_sql(runner, dev_catchment, mock_post):
    _json_response(mock_post, [])
    result = runner.invoke(app, ["query", "dev", "outlet", "--sql", "SELECT 1 AS n"])
    assert result.exit_code == 0
    payload = mock_post.call_args.kwargs["json"]
    assert payload["sql"] == "SELECT 1 AS n"


def test_query_sql_from_file(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("SELECT * FROM outlet.daily LIMIT 5")
    _json_response(mock_post, [])
    result = runner.invoke(app, ["query", "dev", "outlet", "--sql", "@query.sql"])
    assert result.exit_code == 0
    payload = mock_post.call_args.kwargs["json"]
    assert "SELECT * FROM outlet.daily LIMIT 5" in payload["sql"]


def test_query_missing_sql_file_exits(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "dev", "outlet", "--sql", "@nonexistent.sql"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0


def test_query_prints_table(runner, dev_catchment, mock_post):
    rows = [{"id": 1, "label": "alpha"}, {"id": 2, "label": "beta"}]
    _json_response(mock_post, rows)
    result = runner.invoke(app, ["query", "dev", "outlet", "daily"])
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "beta" in result.output


def test_query_empty_result(runner, dev_catchment, mock_post):
    _json_response(mock_post, [])
    result = runner.invoke(app, ["query", "dev", "outlet", "daily"])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_query_csv_writes_file(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    csv_bytes = b"id,label\n1,alpha\n"
    mock_post.return_value.content = csv_bytes
    mock_post.return_value.json.return_value = []
    result = runner.invoke(app, ["query", "dev", "outlet", "daily", "--csv", "out.csv"])
    assert result.exit_code == 0
    out = tmp_path / "ponds" / "outlet" / "daily" / "out.csv"
    assert out.exists()
    assert out.read_bytes() == csv_bytes


def test_query_csv_custom_path(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    mock_post.return_value.content = b"id\n1\n"
    mock_post.return_value.json.return_value = []
    result = runner.invoke(app, ["query", "dev", "outlet", "--sql", "SELECT 1", "--csv", "out.csv", "--path", "."])
    assert result.exit_code == 0
    assert (tmp_path / "out.csv").exists()


def test_query_sends_format_in_payload(runner, dev_catchment, mock_post):
    mock_post.return_value.content = b""
    mock_post.return_value.json.return_value = []
    runner.invoke(app, ["query", "dev", "outlet", "daily", "--json", "out.json"])
    payload = mock_post.call_args.kwargs["json"]
    assert payload["format"] == "json"


def test_query_unknown_catchment_exits(runner, mock_post):
    result = runner.invoke(app, ["query", "nonexistent", "outlet"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0
