"""Change files (a Spout's ``mode=changes``) through the real worker path: the first delivery lands every table
whole, later ones land each window's changes (one row per key with an op for a table with a primary key, the
Z-set for one without), a manifest commits each delivery, replays and a delivery that died part way land nothing
twice, and the reader (``duckstring.landing``) combines a run of deliveries into what the source holds. Then the
same on a Volume reached through the Files API (the fake workspace)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pytest

import duckstring.trickle_io as T
from duckstring import landing
from duckstring.catchment.egress_worker import _egress_spout
from duckstring.catchment.registry import pond_data_dir
from duckstring.dataplane import ParquetDataPlane
from duckstring.egress import landing as egress_landing
from duckstring.egress.destination import parse_destination

pytestmark = pytest.mark.timeout(60)
UTC = timezone.utc
F = [datetime(2026, 6, d, 1, tzinfo=UTC) for d in range(1, 7)]


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """What a Catchment process remembers delivering, empty for every test."""
    monkeypatch.setattr(egress_landing, "_KNOWN", {})


class _Source:
    """The source Pond: a merge Trickle with a key, an append Trickle without one, and a plain table."""

    def __init__(self, tmp_path):
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone='UTC'")
        self.data_dir = pond_data_dir(tmp_path, "sales", 1).root

    def run(self, f, *, orders, events, totals):
        T.merge_table(self.con, "orders", self.con.sql(f"SELECT * FROM (VALUES {orders}) t(id, v)"), f, ("id",))
        T.append_table(self.con, "events", self.con.sql(f"SELECT * FROM (VALUES {events}) t(e, n)"), f, ())
        self.con.execute(f"CREATE OR REPLACE TABLE totals AS SELECT * FROM (VALUES {totals}) t(k, total)")
        ParquetDataPlane().export(self.con, self.data_dir, f=f)


def _deliver(tmp_path, dest, f, **job):
    _egress_spout(tmp_path, {"pond_name": "sales", "major": 1, "table": None, "destination": dest, "mode": "changes",
                             "f": f.isoformat(), **job})


def _rows(rel, *cols):
    return sorted(rel.select(*cols).fetchall())


def _two_runs(tmp_path, dest):
    src = _Source(tmp_path)
    src.run(F[0], orders="(1, 'a'), (2, 'b')", events="('x', 1)", totals="('k', 10)")
    _deliver(tmp_path, dest, F[0])
    src.run(F[1], orders="(1, 'a2'), (3, 'c')", events="('y', 2), ('z', 3)", totals="('k', 10)")
    _deliver(tmp_path, dest, F[1])
    return src


def test_the_first_delivery_is_whole_and_later_ones_are_changes(tmp_path):
    out = tmp_path / "out"
    _two_runs(tmp_path, f"file://{out}")
    first, second = landing.deliveries(str(out))
    assert {t: v["kind"] for t, v in first["tables"].items()} == {"orders": "full", "events": "full", "totals": "full"}
    assert first["previous_f"] is None and second["previous_f"] == F[0].isoformat()
    # a plain table is republished by every run, so it lands whole every time
    assert {t: (v["kind"], v["column"]) for t, v in second["tables"].items()} == {
        "orders": ("changes", "_duckstring_op"), "events": ("changes", "_duckstring_d"), "totals": ("full", None)}
    con = duckdb.connect()
    orders = con.sql(f"SELECT * FROM read_parquet('{out}/{second['tables']['orders']['files'][0]}')")
    assert _rows(orders, "id", "v", "_duckstring_op") == [(1, "a2", 1), (2, "b", -1), (3, "c", 1)]
    assert {r[0] for r in orders.select("_duckstring_f").fetchall()} == {F[1]}
    events = con.sql(f"SELECT * FROM read_parquet('{out}/{second['tables']['events']['files'][0]}')")
    assert _rows(events, "e", "n", "_duckstring_d") == [("y", 2, 1), ("z", 3, 1)]
    assert (out / "orders" / f"{landing.stem(F[0])}.full.parquet").exists()
    assert (out / "_duckstring" / "manifests" / "2026-06-02" / "01" / f"{landing.stem(F[1])}.json").exists()


def test_the_reader_combines_deliveries_into_what_the_source_holds(tmp_path):
    out = tmp_path / "out"
    src = _two_runs(tmp_path, f"file://{out}")
    src.run(F[2], orders="(1, 'a3'), (3, 'c'), (4, 'd')", events="('w', 4)", totals="('k', 30)")
    _deliver(tmp_path, f"file://{out}", F[2])
    src.run(F[3], orders="(3, 'c'), (4, 'd')", events="('v', 5)", totals="('k', 30)")
    _deliver(tmp_path, f"file://{out}", F[3])
    con = duckdb.connect()

    whole = landing.changes(con, str(out), "orders")  # from the first (full) delivery: the table as it stands
    assert whole.reset and whole.pk == ["id"] and whole.f == F[3] and whole.after == F[3].isoformat()
    assert _rows(whole.relation, "id", "v", "_duckstring_op") == [(3, "c", 1), (4, "d", 1)]

    since = landing.changes(con, str(out), "orders", after=F[0])  # one row per key, the newest delivery's
    assert not since.reset
    assert _rows(since.relation, "id", "v", "_duckstring_op") == [(1, "a3", -1), (2, "b", -1), (3, "c", 1),
                                                                   (4, "d", 1)]
    events = landing.changes(con, str(out), "events", after=F[1])  # summed weights
    assert _rows(events.relation, "e", "n", "_duckstring_d") == [("v", 5, 1), ("w", 4, 1)]
    totals = landing.changes(con, str(out), "totals", after=F[0])  # a plain table lands whole: the newest wins
    assert totals.reset and _rows(totals.relation, "k", "total") == [("k", 30)]
    assert landing.changes(con, str(out), "totals", after=F[3]) is None


def test_a_replay_lands_nothing_twice(tmp_path, monkeypatch):
    out = tmp_path / "out"
    _two_runs(tmp_path, f"file://{out}")
    files = sorted(p for p in out.rglob("*") if p.is_file())
    _deliver(tmp_path, f"file://{out}", F[1])  # the same process again
    monkeypatch.setattr(egress_landing, "_KNOWN", {})
    _deliver(tmp_path, f"file://{out}", F[1])  # a restarted Catchment: reads latest.json and the manifest
    assert sorted(p for p in out.rglob("*") if p.is_file()) == files


def test_a_delivery_that_died_before_its_manifest_is_landed_again(tmp_path, monkeypatch):
    out = tmp_path / "out"
    src = _two_runs(tmp_path, f"file://{out}")
    src.run(F[2], orders="(1, 'a3'), (3, 'c')", events="('w', 4)", totals="('k', 10)")
    monkeypatch.setattr(egress_landing.Landing, "commit", lambda self: None)  # the process died before it
    _deliver(tmp_path, f"file://{out}", F[2])
    monkeypatch.undo()
    monkeypatch.setattr(egress_landing, "_KNOWN", {})
    assert json.loads((out / "_duckstring" / "latest.json").read_text()) == {"f": F[1].isoformat(),
                                                                              "pending": F[2].isoformat()}
    src.run(F[3], orders="(1, 'a3'), (3, 'c3')", events="('v', 5)", totals="('k', 10)")
    _deliver(tmp_path, f"file://{out}", F[3])
    delivered = landing.deliveries(str(out))
    assert [m["f"] for m in delivered] == [F[0].isoformat(), F[1].isoformat(), F[3].isoformat()]
    assert delivered[-1]["previous_f"] == F[1].isoformat()  # the window reached back over the lost delivery
    con = duckdb.connect()
    events = landing.changes(con, str(out), "events", after=F[1])
    assert _rows(events.relation, "e", "n", "_duckstring_d") == [("v", 5, 1), ("w", 4, 1)]  # each once
    orders = landing.changes(con, str(out), "orders", after=F[1])
    assert _rows(orders.relation, "id", "v", "_duckstring_op") == [(1, "a3", 1), (3, "c3", 1)]


def test_an_empty_manifest_left_by_a_dead_writer_is_cleared(tmp_path, monkeypatch):
    out = tmp_path / "out"
    src = _two_runs(tmp_path, f"file://{out}")
    src.run(F[2], orders="(1, 'a3'), (3, 'c')", events="('w', 4)", totals="('k', 10)")
    _deliver(tmp_path, f"file://{out}", F[2])
    manifest = out.joinpath(*landing.manifest_parts(F[2]))
    manifest.write_bytes(b"")  # its upload never finished
    assert [m["f"] for m in landing.deliveries(str(out))] == [F[0].isoformat(), F[1].isoformat()]  # stops there
    monkeypatch.setattr(egress_landing, "_KNOWN", {})
    src.run(F[3], orders="(1, 'a3'), (3, 'c')", events="('v', 5)", totals="('k', 10)")
    _deliver(tmp_path, f"file://{out}", F[3])
    assert not manifest.exists()
    assert [m["f"] for m in landing.deliveries(str(out))] == [F[0].isoformat(), F[1].isoformat(), F[3].isoformat()]


def test_an_unchanged_window_writes_only_the_progress(tmp_path):
    out = tmp_path / "out"
    src = _Source(tmp_path)
    for f in F[:2]:
        src.run(f, orders="(1, 'a'), (2, 'b')" if f == F[0] else "(1, 'a2')", events="('x', 1)", totals="('k', 1)")
        _deliver(tmp_path, f"file://{out}", f, table="orders")
    src.run(F[2], orders="(1, 'a2')", events="('x', 1)", totals="('k', 1)")
    files = sorted(p for p in out.rglob("*.parquet"))
    _deliver(tmp_path, f"file://{out}", F[2], table="orders")
    assert sorted(p for p in out.rglob("*.parquet")) == files and len(landing.deliveries(str(out))) == 2
    assert json.loads((out / "_duckstring" / "latest.json").read_text()) == {"f": F[2].isoformat()}


def test_a_resync_lands_every_table_whole_after_what_a_reader_has_applied(tmp_path):
    out = tmp_path / "out"
    src = _two_runs(tmp_path, f"file://{out}")
    con = duckdb.connect()
    applied = landing.changes(con, str(out), "orders").after
    assert applied == F[1].isoformat()
    _deliver(tmp_path, f"file://{out}", F[1], resync=True)  # at the freshness already delivered
    last = landing.deliveries(str(out))[-1]
    assert last["delivery"] == f"{F[1].isoformat()}#1" and {v["kind"] for v in last["tables"].values()} == {"full"}
    assert (out / "orders" / f"{landing.stem(F[1], 1)}.full.parquet").exists()
    again = landing.changes(con, str(out), "orders", after=applied)  # a reader that applied F[1] still sees it
    assert again.reset and again.after == f"{F[1].isoformat()}#1"
    assert _rows(again.relation, "id", "v") == [(1, "a2"), (3, "c")]
    src.run(F[2], orders="(1, 'a2'), (3, 'c'), (9, 'z')", events="('r', 7)", totals="('k', 1)")
    _deliver(tmp_path, f"file://{out}", F[2])  # and the next delivery is changes since F[1] again
    nxt = landing.changes(con, str(out), "orders", after=again.after)
    assert not nxt.reset and _rows(nxt.relation, "id", "v", "_duckstring_op") == [(9, "z", 1)]


def test_change_files_need_a_file_destination(tmp_path):
    _Source(tmp_path).run(F[0], orders="(1, 'a')", events="('x', 1)", totals="('k', 1)")
    with pytest.raises(ValueError, match="mode=changes lands change files"):
        _deliver(tmp_path, "postgres://u@h/db", F[0])
    assert parse_destination("/Volumes/lake/sales/landing").scheme == "file"


# ─── on a Volume through the Files API ───────────────────────────────────────


@pytest.fixture
def ws(monkeypatch):
    pytest.importorskip("fsspec")
    from databricks_fake import FakeWorkspace

    from duckstring import databricks_rest as dbx

    ws = FakeWorkspace(pending_polls=0)
    monkeypatch.setenv("DUCKSTRING_VOLUMES", "api")
    monkeypatch.setenv("DATABRICKS_HOST", "https://adb-1.2.azuredatabricks.net")
    monkeypatch.setenv("DATABRICKS_TOKEN", "pat-x")
    real = dbx.Client

    class Client(real):
        def __init__(self, host, env, *, transport=None, timeout=60.0):
            super().__init__(host, env, transport=transport or ws.transport(), timeout=timeout)

    monkeypatch.setattr(dbx, "Client", Client)
    return ws


def test_change_files_land_on_a_volume_and_read_back(tmp_path, ws):
    vol = "/Volumes/lake/sales/landing"
    src = _two_runs(tmp_path, vol)
    assert f"{vol}/_duckstring/latest.json" in ws.files
    assert not any(".duckstring-next" in k for k in ws.files)  # every shadow removed after its write
    con = duckdb.connect()
    orders = landing.changes(con, vol, "orders", after=F[0])
    assert _rows(orders.relation, "id", "v", "_duckstring_op") == [(1, "a2", 1), (2, "b", -1), (3, "c", 1)]
    ws.calls.clear()
    src.run(F[2], orders="(1, 'a3'), (3, 'c')", events="('w', 4)", totals="('k', 10)")
    _deliver(tmp_path, vol, F[2])
    puts = [k for m, k in ws.calls if m == "PUT"]
    # latest.json through its shadow, one file per table, the manifest once
    assert len(puts) == 2 + 3 + 1 and puts[-1].endswith(f"{landing.stem(F[2])}.json")
    assert not any(m == "GET" for m, _ in ws.calls)  # this process already knows what it delivered
