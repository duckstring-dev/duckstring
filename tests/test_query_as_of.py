"""As-of queries: ``/api/query`` with ``at`` reads a Pond's tables as they stood at a freshness, and with ``since`` as
well returns the change in the query's result between the two points. A Puddle Source reads production this way
(plans/showcase-fixes.md item 10); it is also useful on its own."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb

from duckstring import trickle_io as T
from duckstring.dataplane import KEEP_ALL, ParquetDataPlane

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


class Source:
    """A Pond ``src`` publishing ``fact`` (append ``id, k``), ``dim`` (merge ``k, price``) and ``daily`` (plain
    ``n``) into a Catchment root."""

    def __init__(self, root):
        self.dir = root / "ponds" / "src" / "m1" / "data"
        self.con = duckdb.connect(str(root / "src.duckdb"))

    def run(self, f, *, facts=(), prices=None, daily=None, retain_from=KEEP_ALL):
        con = self.con
        if facts:
            body = ", ".join(f"({i}, '{k}')" for i, k in facts)
            T.append_table(con, "fact", con.sql(f"SELECT * FROM (VALUES {body}) v(id, k)"), f, ("id",))
        if prices is not None:
            body = ", ".join(f"('{k}', {p})" for k, p in prices.items())
            T.merge_table(con, "dim", con.sql(f"SELECT * FROM (VALUES {body}) v(k, price)"), f, ("k",))
        if daily is not None:
            con.execute(f"CREATE OR REPLACE TABLE daily AS SELECT {daily} AS n")
        ParquetDataPlane().export(con, self.dir, f=f, retain_from=retain_from)


def _query(client, sql, **params):
    return client.post("/api/query", json={"pond": "src", "sql": sql, **params})


def _seeded(client):
    src = Source(client.app.state.root)
    src.run(ts(1), facts=[(1, "a"), (2, "b")], prices={"a": 10, "b": 20}, daily=1)
    src.run(ts(2), facts=[(3, "a")], prices={"a": 11, "b": 20, "c": 30}, daily=2)
    src.run(ts(3), facts=[(4, "c")], prices={"a": 11, "c": 30}, daily=3)
    return src


def test_at_reads_each_table_as_it_stood(catchment_client):
    _seeded(catchment_client)
    at = ts(2).isoformat()
    r = _query(catchment_client, "SELECT id, k FROM fact ORDER BY id", at=at)
    assert r.status_code == 200, r.text
    assert [(x["id"], x["k"]) for x in r.json()] == [(1, "a"), (2, "b"), (3, "a")]
    r = _query(catchment_client, 'SELECT k, price FROM "src"."dim" ORDER BY k', at=at)
    assert [(x["k"], x["price"]) for x in r.json()] == [("a", 11), ("b", 20), ("c", 30)]
    r = _query(catchment_client, "SELECT n FROM daily", at=at)
    assert [x["n"] for x in r.json()] == [2]
    # Without at: the newest, as before.
    r = _query(catchment_client, "SELECT k FROM dim ORDER BY k")
    assert [x["k"] for x in r.json()] == ["a", "c"]


def test_since_returns_the_change_in_the_result(catchment_client):
    _seeded(catchment_client)
    r = _query(catchment_client, "SELECT k, price FROM dim", at=ts(3).isoformat(), since=ts(1).isoformat())
    assert r.status_code == 200, r.text
    change = sorted((x["k"], x["price"], x["_duckstring_d"]) for x in r.json())
    # a 10 → 11, b deleted, c added
    assert change == [("a", 10, -1), ("a", 11, 1), ("b", 20, -1), ("c", 30, 1)]

    # A filter is applied at both points, so only the change it keeps travels.
    r = _query(catchment_client, "SELECT id FROM fact WHERE k = 'a'", at=ts(3).isoformat(), since=ts(1).isoformat())
    assert [(x["id"], x["_duckstring_d"]) for x in r.json()] == [(3, 1)]

    # No change: nothing.
    r = _query(catchment_client, "SELECT * FROM daily", at=ts(2).isoformat(), since=ts(2).isoformat())
    assert r.json() == []


def test_since_counts_repeated_rows(catchment_client):
    _seeded(catchment_client)
    # Rows per k: at 01:00 a×1 b×1; at 03:00 a×2 b×1 c×1.
    r = _query(catchment_client, "SELECT k FROM fact", at=ts(3).isoformat(), since=ts(1).isoformat())
    assert sorted((x["k"], x["_duckstring_d"]) for x in r.json()) == [("a", 1), ("c", 1)]


def test_a_change_as_parquet(catchment_client, tmp_path):
    _seeded(catchment_client)
    r = _query(catchment_client, "SELECT k, price FROM dim", at=ts(3).isoformat(), since=ts(2).isoformat(),
               format="parquet")
    assert r.status_code == 200, r.text
    out = tmp_path / "c.parquet"
    out.write_bytes(r.content)
    rows = duckdb.sql(f"SELECT k, price, _duckstring_d FROM '{out}' ORDER BY k").fetchall()
    assert rows == [("b", 20, -1)]
    assert duckdb.sql(f"SELECT typeof(_duckstring_d) FROM '{out}' LIMIT 1").fetchall() == [("BIGINT",)]


def test_a_time_without_an_offset_is_utc(catchment_client):
    _seeded(catchment_client)
    r = _query(catchment_client, "SELECT n FROM daily", at=ts(1).replace(tzinfo=None).isoformat())
    assert [x["n"] for x in r.json()] == [1]


def test_bad_points_are_refused(catchment_client):
    _seeded(catchment_client)
    assert _query(catchment_client, "SELECT 1", since=ts(1).isoformat()).status_code == 422
    assert _query(catchment_client, "SELECT 1", at=ts(1).isoformat(), since=ts(2).isoformat()).status_code == 422
    assert _query(catchment_client, "SELECT 1", at="yesterday").status_code == 422


def test_a_query_with_its_own_weight_column_is_refused(catchment_client):
    _seeded(catchment_client)
    r = _query(catchment_client, "SELECT k, 1 AS _duckstring_d FROM dim", at=ts(3).isoformat(),
               since=ts(1).isoformat())
    assert r.status_code == 422
    assert "_duckstring_d" in r.json()["detail"]


def test_a_pruned_plain_version_reads_the_newest_with_a_warning(catchment_client):
    src = Source(catchment_client.app.state.root)
    src.run(ts(1), daily=1)
    src.run(ts(2), daily=2, retain_from=None)  # keeps only the newest version
    r = _query(catchment_client, "SELECT n FROM daily", at=ts(1).isoformat())
    assert r.status_code == 200, r.text
    assert [x["n"] for x in r.json()] == [2]
    assert "daily" in r.headers["x-duckstring-warning"]


def test_a_trickle_compacted_past_the_point_is_refused(catchment_client):
    src = Source(catchment_client.app.state.root)
    src.run(ts(1), prices={"a": 10})
    src.run(ts(2), prices={"a": 11})
    src.run(ts(3), prices={"a": 12})
    T.checkpoint(src.con, "dim", ts(3))
    ParquetDataPlane().export(src.con, src.dir, f=ts(3) + timedelta(minutes=1), retain_from=ts(3))
    r = _query(catchment_client, "SELECT * FROM dim", at=ts(2).isoformat())
    assert r.status_code == 422, r.text
    assert "holds nothing from before" in r.json()["detail"]
    r = _query(catchment_client, "SELECT price FROM dim", at=ts(3).isoformat())
    assert [x["price"] for x in r.json()] == [12]


def test_a_trickle_created_after_the_point_is_refused(catchment_client):
    """A Trickle's history can't tell "created at 02:00" from "trimmed to 02:00", so a read before it is refused
    either way, saying both."""
    src = Source(catchment_client.app.state.root)
    src.run(ts(1), prices={"a": 10})
    src.run(ts(2), facts=[(1, "a")])
    r = _query(catchment_client, "SELECT count(*) AS n FROM fact", at=ts(1).isoformat())
    assert r.status_code == 422, r.text
    assert "created then, or trimmed since" in r.json()["detail"]
    r = _query(catchment_client, "SELECT price FROM dim", at=ts(1).isoformat())  # a query not naming it runs
    assert r.json() == [{"price": 10}]


def test_the_cli_and_the_python_client_read_as_of(runner, catchment_root, live_catchment):
    from duckstring.cli import app
    from duckstring.core import Catchment

    src = Source(catchment_root)
    src.run(ts(1), prices={"a": 10, "b": 20})
    src.run(ts(2), prices={"a": 11, "b": 20})

    result = runner.invoke(app, ["query", "SELECT k, price FROM dim ORDER BY k", "--pond", "src",
                                 "--at", ts(1).isoformat()])
    assert result.exit_code == 0, result.output
    assert "10" in result.output and "11" not in result.output
    result = runner.invoke(app, ["query", "SELECT k, price FROM dim", "--pond", "src", "--at", ts(2).isoformat(),
                                 "--since", ts(1).isoformat()])
    assert result.exit_code == 0, result.output
    assert "_duckstring_d" in result.output and "20" not in result.output
    assert runner.invoke(app, ["query", "SELECT 1", "--at", ts(1).isoformat()]).exit_code != 0  # needs a Pond
    assert runner.invoke(app, ["query", "SELECT 1", "--pond", "src", "--since", ts(1).isoformat()]).exit_code != 0

    client = Catchment(live_catchment)
    assert client.query("SELECT price FROM dim WHERE k = 'a'", "src", at=ts(1)).fetchall() == [(10,)]
    change = client.query("SELECT k, price FROM dim", "src", at=ts(2), since=ts(1).isoformat())
    assert sorted(change.fetchall()) == [("a", 10, -1), ("a", 11, 1)]
