"""Reading Databricks data into a Pond (``duckstring.databricks``; plans/databricks-bigquery.md, "Reading from
Databricks"), offline: Spark's Parquet output read once committed, a Delta table (a hand-written log), both in place
and through the Files API cache on the fake workspace, a Unity Catalog table through vended credentials and through
the warehouse, and the read position across runs, Rebuilds and a lost registry."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import duckdb
import pytest
from databricks_fake import FakeWorkspace

from duckstring import databricks, delta_log
from duckstring import databricks_rest as dbx
from duckstring.core import Pond
from duckstring.dataplane import publish_plan
from duckstring.engine.core import NEVER
from duckstring.storage import LocalStorage
from duckstring.trickle_io import RUN_KEY, load_sidecar

pytestmark = pytest.mark.timeout(60)
UTC = timezone.utc
F1 = datetime(2026, 6, 1, tzinfo=UTC)
VOL = "/Volumes/lake/sales/inbox"


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_INPUT_CACHE", str(tmp_path / "cache"))
    databricks._clients.clear()
    yield
    databricks._clients.clear()


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


class Runs:
    """A Ripple's runs one after another on one registry: each sees the previous as its previous_f."""

    def __init__(self, tmp_path, own_data_dir=None):
        self.con = duckdb.connect(str(tmp_path / f"registry-{uuid.uuid4().hex[:6]}.duckdb"))
        self.f, self.previous_f, self.own = F1, NEVER, own_data_dir

    def next(self) -> Pond:
        pond = Pond("ingest", "1.0.0", self.con.cursor(), root=None, f=self.f, previous_f=self.previous_f,
                    own_data_dir=self.own)
        self.previous_f, self.f = self.f, self.f + timedelta(hours=1)
        return pond


def ids(rel) -> list:
    return sorted(r[0] for r in rel.select("id").fetchall())


def count(rel) -> int:
    return rel.aggregate("count(*)").fetchall()[0][0]


# ─── Spark's Parquet output ────────────────────────────────────────────────────────────────────────────────


def _parquet(rows) -> bytes:
    con = duckdb.connect()
    path = f"/tmp/ds-{uuid.uuid4().hex}.parquet"
    values = ", ".join(f"({i}, 'v{i}')" for i in rows)
    con.execute(f"COPY (SELECT * FROM (VALUES {values}) t(id, v)) TO '{path}' (FORMAT PARQUET)")
    with open(path, "rb") as fh:
        return fh.read()


def spark_write(put, tid: str, files: dict[str, list], *, removed=(), commit=True, rel=""):
    """What a Spark job on Databricks leaves: data files named for its job, ``_started_<tid>`` and (once
    committed) ``_committed_<tid>`` listing what it added and removed. ``put(path, bytes)`` writes a file."""
    prefix = f"{rel}/" if rel else ""
    names = {f"part-{i:05d}-tid-{tid}-{uuid.uuid4()}-1.c000.snappy.parquet": rows
             for i, rows in enumerate(files.values())}
    put(f"{prefix}_started_{tid}", b"")
    for name, rows in names.items():
        put(prefix + name, _parquet(rows))
    if commit:
        put(f"{prefix}_committed_{tid}", json.dumps({"added": list(names), "removed": list(removed)}).encode())
        put(f"{prefix}_SUCCESS", b"")
    return list(names)


def local_put(root):
    def put(path, data):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return put


def test_spark_output_is_read_once_its_job_commits(tmp_path):
    put = local_put(tmp_path / "out")
    spark_write(put, "111", {"a": [1, 2]})
    spark_write(put, "222", {"a": [3]}, commit=False)  # a job still running (or one that failed)
    put("_temporary/0/part-00000.parquet", _parquet([9]))  # an open-source Spark job's staging
    put("part-00009-empty.parquet", b"")  # a file still uploading: empty on a Volume
    runs = Runs(tmp_path)
    src = databricks.files(runs.next(), str(tmp_path / "out"))
    assert ids(src.read()) == [1, 2] and src.changed and src.via == "files"
    spark_write(put, "222", {}, commit=True)  # the job commits (its data files were already there)
    (tmp_path / "out" / "_committed_222").write_text(json.dumps(
        {"added": [n.name for n in (tmp_path / "out").glob("*tid-222*")], "removed": []}))
    assert ids(databricks.files(runs.next(), str(tmp_path / "out")).read()) == [1, 2, 3]


def test_added_gives_new_files_and_refuses_an_overwrite(tmp_path):
    put, out = local_put(tmp_path / "out"), str(tmp_path / "out")
    first = spark_write(put, "1", {"a": [1, 2], "b": [3]})
    runs = Runs(tmp_path)
    src = databricks.files(runs.next(), out)
    assert ids(src.added()) == [1, 2, 3]  # a first run reads everything
    spark_write(put, "2", {"a": [4]})  # mode("append")
    src = databricks.files(runs.next(), out)
    assert src.changed and ids(src.added()) == [4] and ids(src.read()) == [1, 2, 3, 4]
    src = databricks.files(runs.next(), out)
    assert not src.changed and ids(src.added()) == []
    spark_write(put, "3", {"a": [5]}, removed=first)  # mode("overwrite") of the first job's files
    src = databricks.files(runs.next(), out)
    with pytest.raises(databricks.NotAppendOnly, match="replaced or removed"):
        src.added()
    assert ids(src.read()) == [4, 5]


def test_partition_directories_become_columns(tmp_path):
    put = local_put(tmp_path / "out")
    spark_write(put, "1", {"a": [1]}, rel="g=1")
    spark_write(put, "1", {"a": [2, 3]}, rel="g=2")
    rel = databricks.files(Runs(tmp_path).next(), str(tmp_path / "out")).read()
    assert sorted(rel.select("id", "g").fetchall()) == [(1, 1), (2, 2), (3, 2)]


def test_a_rebuild_reads_everything_again(tmp_path):
    put, out = local_put(tmp_path / "out"), str(tmp_path / "out")
    spark_write(put, "1", {"a": [1]})
    runs = Runs(tmp_path)
    databricks.files(runs.next(), out)
    assert not databricks.files(runs.next(), out).changed
    runs.previous_f = NEVER  # a Rebuild runs with previous_f = NEVER
    src = databricks.files(runs.next(), out)
    assert src.changed and ids(src.added()) == [1]


def test_a_duck_that_lost_its_registry_reads_the_position_back_from_the_run_stamp(tmp_path):
    put, out = local_put(tmp_path / "out"), str(tmp_path / "out")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    spark_write(put, "1", {"a": [1, 2]})
    runs = Runs(tmp_path, own_data_dir=data_dir)
    pond = runs.next()
    src = databricks.files(pond, out)
    publish_plan(runs.con, data_dir, pond.f)  # the run publishes: its input positions go in the run stamp
    stamp = load_sidecar(LocalStorage(data_dir))[RUN_KEY]
    assert stamp["inputs"][out]["version"] == src.version and stamp["f"] == pond.f.isoformat()
    spark_write(put, "2", {"a": [3]})
    fresh = Runs(tmp_path, own_data_dir=data_dir)  # a new Duck: an empty registry
    fresh.f, fresh.previous_f = runs.f, runs.previous_f
    src = databricks.files(fresh.next(), out)
    assert src.changed and ids(src.added()) == [3]


def test_a_failed_run_doesnt_count_as_read(tmp_path):
    put, out = local_put(tmp_path / "out"), str(tmp_path / "out")
    spark_write(put, "1", {"a": [1]})
    runs = Runs(tmp_path)
    databricks.files(runs.next(), out)
    spark_write(put, "2", {"a": [2]})
    databricks.files(runs.next(), out)  # this run fails after reading: it never completes
    runs.previous_f -= timedelta(hours=1)  # so the next run's previous_f is still the first run's
    src = databricks.files(runs.next(), out)
    assert ids(src.added()) == [2]


def test_a_volume_is_read_through_a_cache_that_keeps_what_it_holds(ws, tmp_path):
    def put(path, data):
        ws.files[f"{VOL}/orders/{path}"] = data

    spark_write(put, "1", {"a": [1, 2], "b": [3]})
    runs = Runs(tmp_path)
    src = databricks.files(runs.next(), f"{VOL}/orders")
    assert ids(src.read()) == [1, 2, 3] and src.via == "cache"
    fetched = [k for m, k in ws.calls if m == "GET" and k.endswith(".parquet")]
    assert len(fetched) == 2
    spark_write(put, "2", {"a": [4]})
    ws.calls.clear()
    src = databricks.files(runs.next(), f"{VOL}/orders")
    assert ids(src.added()) == [4]
    assert [k for m, k in ws.calls if m == "GET" and k.endswith(".parquet")] == [
        k for k in ws.files if "tid-2-" in k]  # only the new file


def test_an_empty_commit_marker_is_a_job_still_uploading_it(ws, tmp_path):
    def put(path, data):
        ws.files[f"{VOL}/orders/{path}"] = data

    spark_write(put, "1", {"a": [1]})
    names = spark_write(put, "2", {"a": [2]})
    ws.files[f"{VOL}/orders/_committed_2"] = b""  # visible while its upload runs, empty
    assert ids(databricks.files(Runs(tmp_path).next(), f"{VOL}/orders").read()) == [1]
    assert names


# ─── Delta ─────────────────────────────────────────────────────────────────────────────────────────────────

_SCHEMA = json.dumps({"type": "struct", "fields": [
    {"name": "id", "type": "long", "nullable": True, "metadata": {}},
    {"name": "v", "type": "string", "nullable": True, "metadata": {}}]})


class DeltaTable:
    """A Delta table written by hand: data files from DuckDB, commits as JSON lines."""

    def __init__(self, put):
        self.put, self.version, self.files = put, -1, {}

    def commit(self, *, add=(), remove=(), change=True, first=False):
        actions = []
        if first:
            actions += [{"protocol": {"minReaderVersion": 1, "minWriterVersion": 2}},
                        {"metaData": {"id": str(uuid.uuid4()), "format": {"provider": "parquet", "options": {}},
                                      "schemaString": _SCHEMA, "partitionColumns": [], "configuration": {},
                                      "createdTime": 0}}]
        for name in remove:
            actions.append({"remove": {"path": name, "dataChange": change, "deletionTimestamp": 0}})
        for rows in add:
            name = f"part-{uuid.uuid4()}.c000.snappy.parquet"
            data = _parquet(rows)
            self.put(name, data)
            self.files[name] = rows
            actions.append({"add": {"path": name, "partitionValues": {}, "size": len(data),
                                    "modificationTime": 0, "dataChange": change}})
        self.version += 1
        self.put(f"_delta_log/{self.version:020d}.json", "\n".join(map(json.dumps, actions)).encode())
        return [a["add"]["path"] for a in actions if "add" in a]


@pytest.fixture
def delta_ext():
    con = duckdb.connect()
    try:
        con.execute("INSTALL delta; LOAD delta")
    except duckdb.Error as exc:  # offline with no cached extension
        pytest.skip(f"DuckDB's delta extension is unavailable: {exc}")


@pytest.mark.timeout(120)
def test_a_delta_tables_appends_compactions_and_deletes(tmp_path, delta_ext):
    out = tmp_path / "t"
    table = DeltaTable(local_put(out))
    a, b = table.commit(add=([1, 2], [3]), first=True)
    runs = Runs(tmp_path)
    src = databricks.files(runs.next(), str(out))
    assert src.version == "0" and ids(src.added()) == [1, 2, 3]
    table.commit(add=([4],))  # an append: only the new file is read
    src = databricks.files(runs.next(), str(out))
    assert src.changed and ids(src.added()) == [4]
    (c,) = table.commit(add=([1, 2, 3],), remove=(a, b), change=False)  # OPTIMIZE: rewritten, not changed
    table.commit(add=([5],))
    src = databricks.files(runs.next(), str(out))
    assert src.version == "3" and ids(src.added()) == [5] and ids(src.read()) == [1, 2, 3, 4, 5]
    table.commit(remove=(c,))  # a delete
    src = databricks.files(runs.next(), str(out))
    with pytest.raises(databricks.NotAppendOnly, match="3 rows changed or removed"):
        src.added()
    assert ids(src.read()) == [4, 5]


@pytest.mark.timeout(120)
def test_a_delta_table_on_a_volume_is_mirrored_by_what_its_versions_read(ws, tmp_path, delta_ext):
    def put(path, data):
        ws.files[f"{VOL}/t/{path}"] = data

    table = DeltaTable(put)
    (a,) = table.commit(add=([1, 2],), first=True)
    runs = Runs(tmp_path)
    src = databricks.files(runs.next(), f"{VOL}/t")
    assert ids(src.read()) == [1, 2] and src.via == "cache"
    table.commit(add=([3],))
    table.commit(add=([1, 2, 3],), remove=(a, *[n for n in table.files if n != a]), change=False)
    ws.calls.clear()
    src = databricks.files(runs.next(), f"{VOL}/t")
    assert ids(src.added()) == [3] and ids(src.read()) == [1, 2, 3]
    gets = {k.rsplit("/", 1)[1] for m, k in ws.calls if m == "GET" and k.endswith(".parquet")}
    assert a not in gets  # the first version's file was already held


def test_deletion_vector_files_are_named_from_their_ids():
    # Measured on a Databricks table (2026-11-09): these encoded ids name these files.
    assert delta_log.dv_path({"storageType": "u", "pathOrInlineDv": "sVe^z>:aS>NV&KKx:9ew"}) == \
        "deletion_vector_593647a1-e897-42dc-9a93-88226905d8d8.bin"
    assert delta_log.dv_path({"storageType": "u", "pathOrInlineDv": "ab" + "w%!8.g@-k}L?kQ^mdIMx"}) == \
        "ab/deletion_vector_66988014-34c6-4d08-94d7-f63544f2231c.bin"
    assert delta_log.dv_path({"storageType": "i", "pathOrInlineDv": "x"}) is None


def test_a_checkpoint_replaces_the_commits_before_it(tmp_path):
    log = tmp_path / "t" / "_delta_log"
    log.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(f"""COPY (SELECT {{'path': 'a%20b.parquet', 'size': 1}} AS add, NULL::STRUCT(path VARCHAR) AS remove
                    UNION ALL SELECT {{'path': 'c.parquet', 'size': 1}}, NULL)
                    TO '{log}/00000000000000000002.checkpoint.parquet' (FORMAT PARQUET)""")
    (log / "00000000000000000003.json").write_text("\n".join(map(json.dumps, [
        {"remove": {"path": "c.parquet", "dataChange": True}},
        {"add": {"path": "d.parquet", "dataChange": True, "deletionVector": {
            "storageType": "u", "pathOrInlineDv": "sVe^z>:aS>NV&KKx:9ew", "offset": 1}}}])))
    for v in (0, 1, 2):
        (log / f"{v:020d}.json").write_text("{}")
    store, names = LocalStorage(tmp_path / "t"), sorted(p.name for p in log.iterdir())
    assert delta_log.latest_version(names) == 3 and delta_log.base_checkpoint(names, 3) == 2
    assert delta_log.log_files_for(names, [3]) == ["00000000000000000002.checkpoint.parquet",
                                                    "00000000000000000003.json"]
    data, dvs = delta_log.files_at(con, store, 3, names)
    assert data == {"a b.parquet", "d.parquet"}
    assert dvs == {"deletion_vector_593647a1-e897-42dc-9a93-88226905d8d8.bin"}
    assert delta_log.checkpoints(["00000000000000000004.checkpoint.0000000001.0000000002.parquet"]) == {}
    with pytest.raises(delta_log.DeltaLogError, match="missing commits 1..1"):
        delta_log.log_files_for(["00000000000000000000.json", "00000000000000000002.json"], [2])


# ─── Unity Catalog tables ──────────────────────────────────────────────────────────────────────────────────


def _uc_table(ws, version: int, rows, *, location=None, props=None):
    ws.con.execute("CREATE SCHEMA IF NOT EXISTS lake.sales")
    values = ", ".join(f"({i}, 'v{i}')" for i in rows)
    for name in ("orders", f"orders__v{version}") if rows else ():
        ws.con.execute(f"CREATE OR REPLACE TABLE lake.sales.{name} AS SELECT * FROM (VALUES {values}) t(id, v)")
    ws.uc_tables["lake.sales.orders"] = {
        "full_name": "lake.sales.orders", "table_id": "t-1", "table_type": "MANAGED", "data_source_format": "DELTA",
        "storage_location": location or "abfss://uc@acct.dfs.core.windows.net/t-1",
        "properties": {"delta.lastUpdateVersion": str(version), **(props or {})}}


def test_a_table_is_read_through_the_warehouse_where_storage_isnt_vended(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATABRICKS_WAREHOUSE", "wh-1")
    _uc_table(ws, 0, [1, 2])
    runs = Runs(tmp_path)
    src = databricks.table(runs.next(), "lake.sales.orders")
    assert src.version == "0" and src.changed and ids(src.read()) == [1, 2] and src.via == "warehouse"
    assert ws.executed[-1] == "SELECT * FROM `lake`.`sales`.`orders` VERSION AS OF 0"
    src = databricks.table(runs.next(), "lake.sales.orders")
    assert not src.changed and not ws.executed[-1].startswith("SELECT count")  # unchanged: no statement
    _uc_table(ws, 1, [1, 2, 3])
    src = databricks.table(runs.next(), "lake.sales.orders")
    assert ids(src.added()) == [3]
    _uc_table(ws, 2, [1, 3])
    with pytest.raises(databricks.NotAppendOnly, match="1 rows changed or removed between versions 1 and 2"):
        databricks.table(runs.next(), "lake.sales.orders").added()


def test_without_vended_storage_or_a_warehouse_the_reason_is_given(ws, tmp_path, monkeypatch):
    monkeypatch.delenv("DUCKSTRING_DATABRICKS_WAREHOUSE", raising=False)
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    _uc_table(ws, 0, [1])
    src = databricks.table(Runs(tmp_path).next(), "lake.sales.orders")
    with pytest.raises(dbx.DatabricksError, match="External Data Access.*no SQL warehouse"):
        src.read()


def test_catalog_managed_commits_go_through_the_warehouse(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATABRICKS_WAREHOUSE", "wh-1")
    _uc_table(ws, 0, [1], props={"delta.feature.catalogManaged": "supported"})
    ws.vend = {"azure_user_delegation_sas": {"sas_token": "sv=1"}}
    src = databricks.table(Runs(tmp_path).next(), "lake.sales.orders")
    assert ids(src.read()) == [1] and src.via == "warehouse"


@pytest.mark.timeout(120)
def test_a_table_is_read_from_its_storage_where_unity_catalog_vends_it(ws, tmp_path, delta_ext):
    out = tmp_path / "t-1"
    table = DeltaTable(local_put(out))
    table.commit(add=([1, 2],), first=True)
    _uc_table(ws, 0, [], location=str(out))
    ws.vend = {"azure_user_delegation_sas": {"sas_token": "sv=1"}, "url": str(out)}
    runs = Runs(tmp_path)
    src = databricks.table(runs.next(), "lake.sales.orders")
    assert ids(src.read()) == [1, 2] and src.via == "storage" and not ws.executed  # no warehouse at all
    table.commit(add=([3],))
    _uc_table(ws, 1, [], location=str(out))
    src = databricks.table(runs.next(), "lake.sales.orders")
    assert ids(src.added()) == [3] and not ws.executed
