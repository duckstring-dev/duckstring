"""Volumes through the Files API, for a machine that doesn't mount /Volumes (a Databricks App;
plans/databricks-bigquery.md part 5), offline on the fake workspace: when a /Volumes path takes the API, the
filesystem (including a read that comes back empty during an overwrite), a published layout persisted there and
restored at start, reads that never fall through to it, the state backup, and the writer lease."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest
from databricks_fake import FakeWorkspace

import duckstring.trickle_io as T
from duckstring import databricks_rest as dbx
from duckstring import storage
from duckstring.dataplane import ParquetDataPlane, persist_tree, restore_published
from duckstring.storage import LocalStorage, VolumeStorage, get_storage, volume_api

pytest.importorskip("fsspec")
pytestmark = pytest.mark.timeout(60)
UTC = timezone.utc
F1 = datetime(2026, 6, 1, tzinfo=UTC)
F2 = datetime(2026, 6, 2, tzinfo=UTC)
VOL = "/Volumes/lake/sales/files"


@pytest.fixture
def ws(monkeypatch):
    """The fake workspace behind every Databricks client, with /Volumes taking the Files API."""
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


def test_a_volume_path_takes_the_files_api_only_where_volumes_arent_mounted(monkeypatch, tmp_path):
    monkeypatch.delenv("DUCKSTRING_VOLUMES", raising=False)
    monkeypatch.setattr(storage.os.path, "isdir", lambda p: p == "/Volumes")
    assert not volume_api(VOL)  # mounted (a cluster, a job): a local path
    assert isinstance(get_storage(VOL), LocalStorage)
    monkeypatch.setattr(storage.os.path, "isdir", lambda p: False)
    assert volume_api(VOL)  # a Databricks App
    assert isinstance(get_storage(VOL), VolumeStorage)
    assert not volume_api(str(tmp_path)) and not volume_api("/Volumesque/x")
    monkeypatch.setenv("DUCKSTRING_VOLUMES", "mount")
    assert not volume_api(VOL)


def test_the_filesystem_reads_writes_lists_and_removes(ws):
    store = get_storage(VOL)
    assert not store.duckdb_readable
    store.write_text('{"a": 1}', "t", "_trickle.json")
    store.write_bytes(b"x" * 10, "t", "p", "1.parquet")
    assert store.read_text("t", "_trickle.json") == '{"a": 1}'
    assert store.exists("t", "p", "1.parquet") and store.is_dir("t", "p") and not store.exists("t", "nope")
    assert store.size("t", "p", "1.parquet") == 10 and store.size("t", "nope") == 0
    assert store.child("t").names() == ["_trickle.json"] and store.child("t").subdir_names() == ["p"]
    assert store.parquet_names("t", "p") == ["1.parquet"]
    assert store.read_text("t", "missing.json") is None
    store.child("t").move_into(store.child("t"), "_trickle.json", "moved.json")
    assert store.child("t").names() == ["moved.json"]
    store.remove("t", "moved.json")
    store.remove("t", "moved.json")  # missing is not an error
    store.rmtree("t")
    assert not store.exists("t")
    with pytest.raises(RuntimeError, match="DuckDB can't read"):
        store.uri("t", "p", "1.parquet")


def test_a_read_during_an_overwrite_is_retried(ws, tmp_path):
    store = get_storage(VOL)
    store.write_bytes(b'{"run": 1}', "_trickle.json")
    ws.empty_reads = 3  # the Files API answered 200 with no body while the file was being replaced
    assert store.read_text("_trickle.json") == '{"run": 1}'
    ws.empty_reads = 2
    store.fetch(tmp_path / "s.json", "_trickle.json")
    assert (tmp_path / "s.json").read_bytes() == b'{"run": 1}'
    store.write_bytes(b"", "empty")  # a file that really is empty reads as empty, at once
    ws.empty_reads = 1
    assert store.read_bytes("empty") == b""


def _publish(root, f, orders, totals):
    """One run of a Pond line `sales@1` publishing locally, as a Catchment-local Duck does."""
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(root / "registry.duckdb"))
    con.execute("SET TimeZone='UTC'")
    T.merge_table(con, "orders", con.sql(f"SELECT * FROM (VALUES {orders}) t(id, v)"), f, ("id",))
    con.execute(f"CREATE OR REPLACE TABLE totals AS SELECT * FROM (VALUES {totals}) t(k, total)")
    local = LocalStorage(root / "ponds" / "sales" / "m1" / "data")
    ParquetDataPlane().export(con, local, f=f)
    con.close()
    return local


def _read(local, table, f):
    rel = duckdb.connect().sql(ParquetDataPlane().read_select(local, table, as_of=f))
    return sorted(rel.select(*[c for c in rel.columns if not c.startswith("_duckstring_")]).fetchall())


def test_a_lost_local_publish_is_restored_from_the_volume_at_start(ws, tmp_path):
    first = tmp_path / "first"
    local = _publish(first, F1, "(1, 'a'), (2, 'b')", "('k', 10)")
    plane = get_storage(VOL).child("sales", "m1", "data")
    persist_tree(local, plane)
    local = _publish(first, F2, "(1, 'a2'), (3, 'c')", "('k', 20)")
    persist_tree(local, plane)
    assert plane.exists("_trickle.json")

    second = tmp_path / "second"  # a new machine: nothing local
    restored_files = restore_published(second, VOL)
    assert set(restored_files) == {"sales@1"} and restored_files["sales@1"] > 0
    restored = LocalStorage(second / "ponds" / "sales" / "m1" / "data")
    assert _read(restored, "orders", F2) == [(1, "a2"), (3, "c")]
    assert _read(restored, "totals", F2) == [("k", 20)]
    assert restore_published(second, VOL) == {}  # the local copy is current: nothing to do

    from duckstring.catchment.registry import resolve_data_dir
    got = resolve_data_dir(second, "sales", 1, VOL, expected_f=F2.isoformat())
    assert isinstance(got, LocalStorage)  # never the Volume, which DuckDB can't read


def test_a_stale_local_publish_is_replaced(ws, tmp_path):
    old = _publish(tmp_path / "a", F1, "(1, 'a')", "('k', 10)")
    newer = _publish(tmp_path / "b", F2, "(1, 'b')", "('k', 20)")
    persist_tree(newer, get_storage(VOL).child("sales", "m1", "data"))
    assert set(restore_published(tmp_path / "a", VOL)) == {"sales@1"}
    assert _read(old, "orders", F2) == [(1, "b")]


def test_the_state_backup_round_trips_through_the_volume(ws, tmp_path):
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.state_sync import checkpoint_tier1, restore_state_if_empty

    root = tmp_path / "state"
    root.mkdir()
    con = connect(root / "duck.db")
    migrate(con)
    con.execute("CREATE TABLE marker (v TEXT)")
    con.execute("INSERT INTO marker VALUES ('kept')")
    con.commit()
    backup = f"{VOL}/catchment/state"
    assert checkpoint_tier1(root, backup)
    fresh = tmp_path / "fresh"
    assert restore_state_if_empty(fresh, backup)
    assert connect(fresh / "duck.db").execute("SELECT v FROM marker").fetchall() == [("kept",)]


def test_the_writer_lease_lives_on_the_volume(ws):
    from duckstring.catchment.data_lease import LeaseConflict, acquire_lease

    store = get_storage(VOL)
    acquire_lease(store, "catchment-a")
    acquire_lease(store, "catchment-a")  # a restart of the same Catchment reclaims it
    with pytest.raises(LeaseConflict):
        acquire_lease(store, "catchment-b")


def test_a_catchment_on_a_volume_restores_local_publishes_when_it_starts(ws, tmp_path, capsys):
    from duckstring.catchment.app import create_app

    persist_tree(_publish(tmp_path / "old", F1, "(1, 'a')", "('k', 10)"), get_storage(VOL).child("sales", "m1", "data"))
    root = tmp_path / "catchment"
    create_app(root, data_root=VOL)
    assert "restored the local publish of 1 line(s)" in capsys.readouterr().out
    assert _read(LocalStorage(root / "ponds" / "sales" / "m1" / "data"), "orders", F1) == [(1, "a")]
    assert get_storage(VOL).exists("_duckstring_owner.json")  # the writer lease
