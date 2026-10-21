"""Taps (plans/taps.md): an app's access point on a Pond. Its own key reads only its tables, and its queries
send the Pond a Refresh by the floating Refresh's clock. Covers the policy, the key (scope, rotation, removal,
open mode), the freshness route, the three front doors (HTTP, Postgres wire, Flight), the counters and persistence, and
the regression for the old tap-on-get: no other reader ever sends demand."""

from __future__ import annotations

from datetime import timedelta

import duckdb
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from trickle.io import first_row

from duckstring.catchment import auth
from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.registry import pond_data_dir
from duckstring.catchment.routes import router
from duckstring.catchment.routes.deploy import _register
from duckstring.engine import NEVER, max_demand
from duckstring.keys import pond_key

pytestmark = pytest.mark.timeout(15)

_RIPPLES = [{"func": "f", "name": "r", "parents": []}]
KEY = pond_key("sales", 1)


def _setup(tmp_path, *, keys: bool = False):
    """An Inlet ``sales`` publishing ``revenue`` (exposed, 7 rows) and ``secret_internal`` (hidden), served
    through the API with NoopLauncher (Runs start but never finish unless a test finishes them)."""
    db = connect(tmp_path / "duck.db")
    migrate(db)
    cfg = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "outlet",
           "serve_tables": ["revenue", "daily"]}
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", cfg, _RIPPLES)
    pv = first_row(db.execute("SELECT id FROM pond_version WHERE version = '1.0.0'"))[0]
    for t in ("revenue", "daily", "secret_internal"):
        db.execute('INSERT OR IGNORE INTO pond_version_schema (pond_version_id, "table", "column", type) '
                   "VALUES (?, ?, 'id', 'INTEGER')", (pv, t))
    db.commit()
    data_dir = pond_data_dir(tmp_path, "sales", 1, None)
    data_dir.mkdir()
    con = duckdb.connect()
    for table, n in (("revenue", 7), ("daily", 2), ("secret_internal", 3)):
        with data_dir.copy_to(f"{table}.parquet") as uri:
            con.execute(f"COPY (SELECT range AS id FROM range({n})) TO '{uri}' (FORMAT PARQUET)")
    con.close()
    driver = Driver(db, tmp_path, "http://x", NoopLauncher())
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.driver, app.state.db, app.state.root, app.state.data_root = driver, db, tmp_path, None
    ladder = auth.generate(db) if keys else {}
    return TestClient(app), driver, ladder


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _finish_run(driver) -> None:
    """Complete the Pond Run in flight, as a Duck would (NoopLauncher never does)."""
    with driver.lock:
        ps = driver.state.pond_states[KEY]
        ps.end_f = ps.start_f
        ps.changed_f = ps.start_f


# ─── the policy ──────────────────────────────────────────────────────────────


def test_fresh_within_zero_refreshes_until_a_request_is_waiting(tmp_path):
    _, driver, _ = _setup(tmp_path)
    _, _key = driver.add_tap(KEY, "app")
    scope = auth.TapScope(KEY, "app")
    ps = lambda: driver.state.pond_states[KEY]  # noqa: E731

    driver.tap_query(scope)  # the first query: the Inlet starts a Run at once
    assert ps().start_f != NEVER and not ps().demand
    driver.tap_query(scope)  # a Run is in flight: one request waits behind it
    waiting = max_demand(ps().demand)
    assert waiting is not None and waiting > ps().start_f
    for _ in range(5):       # later queries ride on the waiting request
        driver.tap_query(scope)
    assert ps().demand == [waiting]
    tap = driver.list_taps(KEY)[0]
    assert (tap["query_count"], tap["refresh_count"]) == (7, 2)


def test_fresh_within_counts_from_the_newest_request_or_the_freshness(tmp_path):
    _, driver, _ = _setup(tmp_path)
    driver.add_tap(KEY, "app", fresh_within=timedelta(hours=1))
    scope = auth.TapScope(KEY, "app")
    driver.tap_query(scope)          # never run: due at once
    _finish_run(driver)
    for _ in range(3):               # fresh within the hour: nothing more
        driver.tap_query(scope)
    assert driver.list_taps(KEY)[0]["refresh_count"] == 1
    with driver.lock:                # an hour on, it is due again
        for attr in ("start_f", "end_f", "changed_f"):
            setattr(driver.state.pond_states[KEY], attr, driver.state.pond_states[KEY].end_f - timedelta(hours=2))
    driver.tap_query(scope)
    assert driver.list_taps(KEY)[0]["refresh_count"] == 2


def test_other_demand_counts_as_recent(tmp_path):
    _, driver, _ = _setup(tmp_path)
    driver.add_tap(KEY, "app", fresh_within=timedelta(hours=1))
    driver.refresh_now(KEY)          # a manual refresh starts a Run
    driver.tap_query(auth.TapScope(KEY, "app"))
    assert driver.list_taps(KEY)[0]["refresh_count"] == 0


def test_a_blocked_pond_is_served_but_not_refreshed(tmp_path):
    _, driver, _ = _setup(tmp_path)
    driver.add_tap(KEY, "app")
    with driver.lock:
        driver.state.pond_states[KEY].is_blocked = True
    assert driver.tap_query(auth.TapScope(KEY, "app")) is False
    assert driver.list_taps(KEY)[0]["refresh_count"] == 0


# ─── the key ─────────────────────────────────────────────────────────────────


def test_a_tap_key_reads_its_ponds_exposed_tables_and_nothing_else(tmp_path):
    client, driver, ladder = _setup(tmp_path, keys=True)
    _, key = driver.add_tap(KEY, "app")
    h = _bearer(key)

    # Its tables by bare name, by the Pond's schema and by the major's, with no Pond named.
    r = client.post("/api/query", json={"sql": "SELECT count(*) AS n FROM revenue"}, headers=h)
    assert r.status_code == 200 and r.json() == [{"n": 7}]
    assert client.post("/api/serve/query", json={"sql": "SELECT count(*) AS n FROM sales_v1.revenue"},
                       headers=h).json()["rows"] == [[7]]
    assert client.post("/api/query", json={"sql": "SELECT count(*) AS n FROM daily"}, headers=h).json() == [{"n": 2}]
    # Not a hidden table, nor the files underneath.
    for sql in ("SELECT * FROM sales.secret_internal",
                f"SELECT * FROM read_parquet('{tmp_path}/ponds/sales/m1/data/secret_internal.parquet')"):
        assert client.post("/api/query", json={"sql": sql}, headers=h).status_code == 400
    # Another Pond named is refused; so is every route but the query ones.
    assert client.post("/api/query", json={"pond": "other", "sql": "SELECT 1"}, headers=h).status_code == 403
    assert client.get("/api/status", headers=h).status_code == 403
    assert client.post("/api/ponds/sales/refresh", headers=h).status_code == 403
    assert client.get("/api/ponds/sales/taps", headers=h).status_code == 403
    # The catalog lists its own Pond and tables only.
    cat = client.get("/api/serve", headers=h).json()
    assert [p["name"] for p in cat["ponds"]] == ["sales"]
    assert sorted(t["table"] for t in cat["ponds"][0]["tables"]) == ["daily", "revenue"]
    # Managing Taps takes a full key.
    assert client.get("/api/ponds/sales/taps", headers=_bearer(ladder["read"])).status_code == 403
    assert client.get("/api/ponds/sales/taps", headers=_bearer(ladder["full"])).status_code == 200


def test_only_a_tap_key_sends_demand(tmp_path):
    """The old tap-on-get fired on every reader. A query with any other key, or in the web UI's data viewer,
    leaves the Pond alone."""
    client, driver, ladder = _setup(tmp_path, keys=True)
    driver.add_tap(KEY, "app")
    for k in (ladder["read"], ladder["full"]):
        h = _bearer(k)
        assert client.post("/api/query", json={"pond": "sales", "sql": "SELECT 1"}, headers=h).status_code == 200
        assert client.post("/api/serve/query", json={"sql": "SELECT 1"}, headers=h).status_code == 200
        assert client.post("/api/query/page", json={"pond": "sales", "table": "revenue"},
                           headers=h).status_code == 200
    ps = driver.state.pond_states[KEY]
    assert ps.start_f == NEVER and not ps.demand
    assert driver.list_taps(KEY)[0]["query_count"] == 0


def test_rotate_and_remove_end_the_old_key(tmp_path):
    client, driver, ladder = _setup(tmp_path, keys=True)
    full = _bearer(ladder["full"])
    resp = client.post("/api/ponds/sales/taps", json={"name": "app", "tables": ["revenue"]}, headers=full).json()
    old = resp["key"]
    assert old.startswith("tap_")
    assert client.post("/api/query", json={"sql": "SELECT 1"}, headers=_bearer(old)).status_code == 200

    new = client.post("/api/ponds/sales/taps/app/rotate", headers=full).json()["key"]
    assert client.post("/api/query", json={"sql": "SELECT 1"}, headers=_bearer(old)).status_code == 401
    assert client.post("/api/query", json={"sql": "SELECT 1"}, headers=_bearer(new)).status_code == 200

    assert client.post("/api/ponds/sales/taps/app/remove", headers=full).status_code == 200
    assert client.post("/api/query", json={"sql": "SELECT 1"}, headers=_bearer(new)).status_code == 401
    assert client.post("/api/ponds/sales/taps/app/remove", headers=full).status_code == 404


def test_a_tap_key_works_on_an_open_catchment(tmp_path):
    client, driver, _ = _setup(tmp_path)
    _, key = driver.add_tap(KEY, "app")
    assert client.post("/api/query", json={"sql": "SELECT count(*) AS n FROM revenue"},
                       headers=_bearer(key)).json() == [{"n": 7}]
    assert driver.list_taps(KEY)[0]["refresh_count"] == 1
    # Without the key the request is the open Catchment's own, and sends nothing more.
    client.post("/api/query", json={"pond": "sales", "sql": "SELECT 1"})
    assert driver.list_taps(KEY)[0]["query_count"] == 1


def test_add_validates(tmp_path):
    client, driver, _ = _setup(tmp_path)
    assert client.post("/api/ponds/sales/taps", json={"name": "bad name"}).status_code == 422
    assert client.post("/api/ponds/sales/taps", json={"name": "a"}).status_code == 200
    assert client.post("/api/ponds/sales/taps", json={"name": "a"}).status_code == 422  # duplicate


# ─── freshness ───────────────────────────────────────────────────────────────


def test_the_freshness_route_tells_an_app_how_current_its_data_is(tmp_path):
    from datetime import datetime, timezone

    client, driver, ladder = _setup(tmp_path, keys=True)
    _, key = driver.add_tap(KEY, "app")
    sidecar = tmp_path / "ponds" / "sales" / "m1" / "data" / "_trickle.json"
    sidecar.write_text('{"revenue": {"mode": "overwrite", "f": "2026-10-17T06:00:00+00:00"}, '
                       '"secret_internal": {"mode": "overwrite", "f": "2026-10-17T06:00:00+00:00"}}')

    fr = client.get("/api/ponds/sales/freshness", headers=_bearer(key)).json()  # never run
    assert (fr["freshness"], fr["changed"], fr["refreshing"]) == (None, None, False)
    client.post("/api/query", json={"sql": "SELECT 1"}, headers=_bearer(key))  # the Tap's query starts a Run
    assert client.get("/api/ponds/sales/freshness", headers=_bearer(key)).json()["refreshing"] is True
    _finish_run(driver)
    fr = client.get("/api/ponds/sales/freshness", headers=_bearer(key)).json()
    assert fr["refreshing"] is False and datetime.fromisoformat(fr["freshness"]) <= datetime.now(timezone.utc)
    assert fr["freshness"] == fr["changed"]
    # A Tap's key sees its Pond's exposed tables, and no Objects; it can't ask about another Pond.
    assert fr["tables"] == {"revenue": "2026-10-17T06:00:00+00:00"} and "objects" not in fr
    assert client.get("/api/ponds/other/freshness", headers=_bearer(key)).status_code == 403
    # A full key sees every table, and the Objects.
    fr = client.get("/api/ponds/sales/freshness", headers=_bearer(ladder["full"])).json()
    assert set(fr["tables"]) == {"revenue", "secret_internal"} and fr["objects"] == {}


# ─── the wires ───────────────────────────────────────────────────────────────


def test_the_pg_wire_takes_a_tap_key_as_the_password(tmp_path):
    pg8000 = pytest.importorskip("pg8000.native")
    from duckstring.catchment.pg_wire import PgWireServer

    _, driver, ladder = _setup(tmp_path, keys=True)
    _, key = driver.add_tap(KEY, "app")
    server = PgWireServer(driver, host="127.0.0.1", port=0)
    port = server.start()
    try:
        conn = pg8000.Connection("app", host="127.0.0.1", port=port, database="d", password=key)
        assert conn.run("SELECT count(*) FROM revenue") == [[7]]
        from pg8000.native import DatabaseError
        with pytest.raises(DatabaseError):
            conn.run("SELECT * FROM secret_internal")
        conn.close()
        # Two queries: one started a Run, the other queued a request behind it.
        assert driver.list_taps(KEY)[0]["refresh_count"] == 2
        # A read key on the same wire sends nothing.
        conn = pg8000.Connection("analyst", host="127.0.0.1", port=port, database="d", password=ladder["read"])
        conn.run("SELECT count(*) FROM sales.revenue")
        conn.close()
        assert driver.list_taps(KEY)[0]["query_count"] == 2
    finally:
        server.stop()


def test_flight_takes_a_tap_key_as_the_bearer(tmp_path):
    flight = pytest.importorskip("pyarrow.flight")
    from duckstring.catchment.flight_sql import FlightSqlServer

    _, driver, _ = _setup(tmp_path, keys=True)
    _, key = driver.add_tap(KEY, "app")
    server = FlightSqlServer(driver, host="127.0.0.1", port=0)
    server.start()
    try:
        client = flight.FlightClient(f"grpc://127.0.0.1:{server.port}")
        options = flight.FlightCallOptions(headers=[(b"authorization", f"Bearer {key}".encode())])
        table = client.do_get(flight.Ticket(b"SELECT count(*) AS n FROM revenue"), options).read_all()
        assert table.column("n").to_pylist() == [7]
        assert driver.list_taps(KEY)[0]["refresh_count"] == 1
    finally:
        server.stop()


# ─── counters, persistence, metrics, status ──────────────────────────────────


def test_counters_are_flushed_and_taps_survive_a_reload(tmp_path):
    _, driver, _ = _setup(tmp_path)
    _, key = driver.add_tap(KEY, "app", fresh_within=timedelta(minutes=5))
    driver.tap_query(auth.TapScope(KEY, "app"))
    row = first_row(driver.db.execute("SELECT query_count FROM pond_tap"))
    assert row == (0,)  # not written per query
    driver.scheduler_tick()
    assert first_row(driver.db.execute("SELECT query_count, refresh_count FROM pond_tap")) == (1, 1)

    fresh = Driver(driver.db, tmp_path, "http://x", NoopLauncher())
    assert fresh.tap_for_hash(auth.hash_key(key)) == auth.TapScope(KEY, "app")
    (tap,) = fresh.list_taps(KEY)
    assert (tap["fresh_within_ms"], tap["query_count"]) == (300000, 1)


def test_status_and_metrics_show_taps_but_never_keys(tmp_path):
    from duckstring.catchment.routes.metrics import render_metrics

    _, driver, _ = _setup(tmp_path)
    _, key = driver.add_tap(KEY, "app")
    driver.tap_query(auth.TapScope(KEY, "app"))
    entry = next(p for p in driver.status()["ponds"] if p["id"] == KEY)
    assert [t["name"] for t in entry["taps"]] == ["app"]
    assert key not in str(entry) and auth.hash_key(key) not in str(entry)
    text = render_metrics(driver.metrics_snapshot())
    assert 'duckstring_tap_queries_total{pond="sales",major="1",tap="app"} 1' in text
    assert 'duckstring_tap_refreshes_total{pond="sales",major="1",tap="app"} 1' in text


def test_removing_the_line_removes_its_taps(tmp_path):
    _, driver, _ = _setup(tmp_path)
    _, key = driver.add_tap(KEY, "app")
    driver.remove_pond("sales", 1)
    assert first_row(driver.db.execute("SELECT count(*) FROM pond_tap")) == (0,)
    assert driver.tap_for_hash(auth.hash_key(key)) is None


# ─── the CLI ─────────────────────────────────────────────────────────────────


@pytest.mark.timeout(30)
def test_cli_tap_roundtrip(runner, live_catchment):
    import re

    from test_spout import _deploy_outlet

    from duckstring.cli import app as cli_app

    _deploy_outlet(live_catchment)  # registers 'dev' as the default catchment

    res = runner.invoke(cli_app, ["refresh", "tap", "add", "sales", "--name", "app", "--fresh-within", "5m"])
    assert res.exit_code == 0, res.output
    key = re.search(r"tap_\S+", res.output).group(0)

    res = runner.invoke(cli_app, ["refresh", "tap", "ls", "sales"])
    assert res.exit_code == 0, res.output
    assert "app" in res.output and "5m" in res.output and "never" in res.output

    res = runner.invoke(cli_app, ["refresh", "tap", "rotate", "sales", "app"])
    assert res.exit_code == 0, res.output
    assert key not in res.output and re.search(r"tap_\S+", res.output)

    res = runner.invoke(cli_app, ["refresh", "tap", "rm", "sales", "app"])
    assert res.exit_code == 0, res.output
    assert "No Taps" in runner.invoke(cli_app, ["refresh", "tap", "ls", "sales"]).output
