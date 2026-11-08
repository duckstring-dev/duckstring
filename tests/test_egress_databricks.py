"""Databricks egress: a Pond's tables delivered into Unity Catalog tables through the real worker path, offline
(plans/databricks-bigquery.md, part 4). A fake workspace (``databricks_fake``) runs the warehouse's statements on
DuckDB. Every kind of source (merge, append, overwrite), staging through the Volume and removing it after, the
watermark, replays that change nothing, a column the source gains, an empty window, and the destination's options."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest
from databricks_fake import FakeWorkspace

import duckstring.trickle_io as T
from duckstring import databricks_rest as dbx
from duckstring.catchment.egress_worker import _egress_spout
from duckstring.catchment.registry import pond_data_dir
from duckstring.dataplane import ParquetDataPlane
from duckstring.egress import get_egress
from duckstring.egress.destination import DestinationError, parse_destination

pytest.importorskip("sqlglot")
pytestmark = pytest.mark.timeout(60)
UTC = timezone.utc
F1 = datetime(2026, 6, 1, tzinfo=UTC)
F2 = datetime(2026, 6, 2, tzinfo=UTC)
F3 = datetime(2026, 6, 3, tzinfo=UTC)
DEST = "databricks://adb-1.2.azuredatabricks.net/lake.sales?warehouse=wh1&volume=/Volumes/lake/sales/staging" \
       "&token=${env:DBX_TOKEN}"


@pytest.fixture
def ws(monkeypatch):
    """The fake workspace, reached by every Databricks client the driver makes."""
    ws = FakeWorkspace(pending_polls=0)
    ws.con.execute("CREATE SCHEMA lake.sales")
    monkeypatch.setenv("DBX_TOKEN", "pat-x")
    real = dbx.Client

    class Client(real):
        def __init__(self, host, env, *, transport=None, timeout=60.0):
            super().__init__(host, env, transport=transport or ws.transport(), timeout=timeout)

    monkeypatch.setattr(dbx, "Client", Client)
    return ws


class _Source:
    """The source Pond: a registry connection plus its published data directory."""

    def __init__(self, tmp_path):
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone='UTC'")
        self.data_dir = pond_data_dir(tmp_path, "sales", 1).root

    def run(self, f, *, orders, events, totals):
        T.merge_table(self.con, "orders", self.con.sql(f"SELECT * FROM (VALUES {orders}) t(id, v)"), f, ("id",))
        T.append_table(self.con, "events", self.con.sql(f"SELECT * FROM (VALUES {events}) t(e, n)"), f, ())
        self.con.execute(f"CREATE OR REPLACE TABLE totals AS SELECT * FROM (VALUES {totals}) t(k, total)")
        ParquetDataPlane().export(self.con, self.data_dir, f=f)


def _deliver(tmp_path, f, **job):
    _egress_spout(tmp_path, {"pond_name": "sales", "major": 1, "table": None, "destination": DEST,
                             "f": f.isoformat(), **job})


def test_every_source_lands_and_the_staging_is_removed(tmp_path, ws):
    src = _Source(tmp_path)
    src.run(F1, orders="(1, 'a'), (2, 'b')", events="('e1', 1)", totals="('k', 10)")
    _deliver(tmp_path, F1)
    assert ws.table("lake.sales.orders") == [(1, "a"), (2, "b")]
    assert ws.table("lake.sales.events") == [("e1", 1)]
    assert ws.table("lake.sales.totals") == [("k", 10)]

    src.run(F2, orders="(1, 'a2'), (3, 'c')", events="('e2', 2)", totals="('k', 20)")
    _deliver(tmp_path, F2)
    assert ws.table("lake.sales.orders") == [(1, "a2"), (3, "c")]  # 1 changed, 2 removed, 3 added: one MERGE
    assert ws.table("lake.sales.events") == [("e1", 1, None), ("e2", 2, F2.isoformat())]  # stamped from the delta
    assert ws.table("lake.sales.totals") == [("k", 20)]
    assert dict(ws.table("lake.sales._duckstring_egress")) == {t: F2.isoformat() for t in ("orders", "events", "totals")}
    assert not ws.files  # every staged file was removed from the Volume
    assert any(s.startswith("MERGE INTO `lake`.`sales`.`orders`") for s in ws.executed)


def test_a_replayed_window_changes_nothing(tmp_path, ws):
    src = _Source(tmp_path)
    src.run(F1, orders="(1, 'a')", events="('e1', 1)", totals="('k', 10)")
    _deliver(tmp_path, F1)
    src.run(F2, orders="(1, 'b'), (2, 'c')", events="('e2', 2), ('e3', 3)", totals="('k', 20)")
    _deliver(tmp_path, F2)
    before = {t: ws.table(f"lake.sales.{t}") for t in ("orders", "events", "totals")}
    # A crash after the data and before the watermark: the watermark still says F1, so the window comes again.
    ws.con.execute("UPDATE lake.sales._duckstring_egress SET f = ?", [F1.isoformat()])
    _deliver(tmp_path, F2)
    assert {t: ws.table(f"lake.sales.{t}") for t in ("orders", "events", "totals")} == before
    n = len(ws.executed)
    _deliver(tmp_path, F2)  # the watermark covers it now: nothing is written
    assert all(s.startswith("CREATE TABLE IF NOT EXISTS") or s.startswith("SELECT f FROM")
               for s in ws.executed[n:])


def test_a_column_the_source_gains_is_added(tmp_path, ws):
    src = _Source(tmp_path)
    src.con.execute("CREATE TABLE totals AS SELECT 'all' AS k, 10::INTEGER AS total")
    ParquetDataPlane().export(src.con, src.data_dir, f=F1)
    _deliver(tmp_path, F1, table="totals")
    src.con.execute("CREATE OR REPLACE TABLE totals AS SELECT 'all' AS k, 20::INTEGER AS total, 'eur' AS currency")
    ParquetDataPlane().export(src.con, src.data_dir, f=F2)
    _deliver(tmp_path, F2, table="totals")
    assert ws.table("lake.sales.totals") == [("all", 20, "eur")]
    assert any(s.startswith("ALTER TABLE `lake`.`sales`.`totals` ADD COLUMNS (`currency` STRING)") for s in ws.executed)


def test_an_unchanged_window_only_moves_the_watermark(tmp_path, ws):
    src = _Source(tmp_path)
    src.run(F1, orders="(1, 'a')", events="('e1', 1)", totals="('k', 10)")
    _deliver(tmp_path, F1)
    src.run(F2, orders="(1, 'a')", events="('e1', 1)", totals="('k', 10)")  # the same state, nothing appended
    n = len(ws.executed)
    _deliver(tmp_path, F2, table="orders")
    later = ws.executed[n:]
    assert not any(s.startswith(("MERGE INTO `lake`.`sales`.`orders`", "INSERT")) for s in later)
    assert dict(ws.table("lake.sales._duckstring_egress"))["orders"] == F2.isoformat()


def test_the_destination_names_schema_warehouse_and_volume(monkeypatch):
    parse_destination(DEST)  # a known scheme
    for bad, why in (("databricks://h/lake?warehouse=w&volume=/Volumes/a/b/c", "catalog.schema"),
                     ("databricks://h/lake.sales?volume=/Volumes/a/b/c", "warehouse="),
                     ("databricks://h/lake.sales?warehouse=w&volume=/tmp/x", "Unity Catalog Volume")):
        with pytest.raises(DestinationError, match=why):
            get_egress(bad).config()
    monkeypatch.setenv("DATABRICKS_HOST", "https://adb-9.9.azuredatabricks.net")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "app-sp")
    cfg = get_egress("databricks:///lake.sales?warehouse=w&volume=/Volumes/a/b/c").config()
    assert cfg["host"] == "https://adb-9.9.azuredatabricks.net" and cfg["env"]["DATABRICKS_CLIENT_ID"] == "app-sp"
    assert get_egress(DEST).capabilities().requires_pk is False


def test_the_test_button_writes_nothing_but_a_removed_probe(tmp_path, ws):
    get_egress(DEST).test_connection(duckdb.connect())
    assert ws.executed == ["SELECT 1"] and not ws.files
    assert set(ws.auth) == {"Bearer pat-x"}
