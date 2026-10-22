"""DuckLake egress: a Pond's tables delivered into a DuckLake through the real worker path, offline (a SQLite
catalog and a local data path). Every kind of source (merge, append, overwrite), one snapshot per run labelled
with the Pond, exactly-once through the in-lake watermark, all-or-nothing when a table fails, an additive
schema change, and the destination string's options."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pytest

import duckstring.trickle_io as T
from duckstring.catchment.egress_worker import _egress_spout
from duckstring.catchment.registry import pond_data_dir
from duckstring.dataplane import ParquetDataPlane
from duckstring.egress import get_egress

pytestmark = pytest.mark.timeout(60)
UTC = timezone.utc
F1 = datetime(2026, 6, 1, tzinfo=UTC)
F2 = datetime(2026, 6, 2, tzinfo=UTC)
F3 = datetime(2026, 6, 3, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _ducklake():
    try:
        duckdb.connect().execute("INSTALL ducklake; LOAD ducklake")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"the ducklake extension isn't available ({exc})")


def _con():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


class _Source:
    """The source Pond: a registry connection plus its published data directory."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.con = _con()
        self.data_dir = pond_data_dir(tmp_path, "sales", 1).root

    def publish(self, f):
        ParquetDataPlane().export(self.con, self.data_dir, f=f)

    def run(self, f, *, orders, events, totals):
        """One run: ``orders`` a merge Trickle (complete state), ``events`` appended, ``totals`` overwritten."""
        T.merge_table(self.con, "orders", self.con.sql(f"SELECT * FROM (VALUES {orders}) t(id, v)"), f, ("id",))
        T.append_table(self.con, "events", self.con.sql(f"SELECT * FROM (VALUES {events}) t(e, n)"), f, ())
        self.con.execute(f"CREATE OR REPLACE TABLE totals AS SELECT * FROM (VALUES {totals}) t(k, total)")
        self.publish(f)


def _lake(tmp_path) -> str:
    return f"ducklake:sqlite:{tmp_path / 'meta.sqlite'}?data_path={tmp_path / 'lake'}/"


def _deliver(tmp_path, f, **job):
    _egress_spout(tmp_path, {"pond_name": "sales", "major": 1, "table": None, "destination": _lake(tmp_path),
                             "f": f.isoformat(), **job})


def _read(tmp_path):
    """The lake as a separate reader sees it: its tables, snapshots and watermarks."""
    con = _con()
    con.execute(f"ATTACH 'ducklake:sqlite:{tmp_path / 'meta.sqlite'}' AS r (READ_ONLY)")
    tables = {t: sorted(con.execute(f'SELECT * FROM r.main."{t}"').fetchall()) for t in ("orders", "events", "totals")}
    snaps = con.execute("SELECT author, commit_message, commit_extra_info FROM r.snapshots() "
                        "WHERE author IS NOT NULL ORDER BY snapshot_id").fetchall()
    marks = dict(con.execute("SELECT table_name, f FROM r.main._duckstring_egress").fetchall())
    con.close()
    return tables, snaps, marks


def test_the_scheme_takes_any_source():
    caps = get_egress("ducklake:sqlite:/srv/meta.sqlite").capabilities()
    assert (caps.supports_delta, caps.transactional, caps.requires_pk) == (True, True, False)


def test_spout_options_leave_the_attach_string(monkeypatch):
    monkeypatch.setenv("LAKE_KEY", "AKIA123")
    drv = get_egress("ducklake:postgres:postgresql://u@h/lake?sslmode=require&data_path=s3://b/lake/"
                     "&schema=sales&key_id=${env:LAKE_KEY}&region=eu-west-1")
    attach, options = drv._parts()
    assert attach == "ducklake:postgres:postgresql://u@h/lake?sslmode=require"
    assert options == {"data_path": "s3://b/lake/", "schema": "sales", "key_id": "AKIA123", "region": "eu-west-1"}
    assert drv._schema() == "sales"


def test_an_attach_error_keeps_its_detail_and_masks_credentials(monkeypatch):
    monkeypatch.setenv("LAKE_PG", "hunter2")
    drv = get_egress("ducklake:postgres:dbname=lake host=h password=${env:LAKE_PG}")
    masked = drv._redact("could not connect: dbname=lake host=h password=hunter2 (postgresql://u:pw2@h/x)")
    assert "hunter2" not in masked and "pw2" not in masked and "could not connect" in masked


def test_every_source_lands_as_one_snapshot_per_run(tmp_path):
    src = _Source(tmp_path)
    src.run(F1, orders="(1, 'a'), (2, 'b')", events="('x', 1)", totals="('all', 10)")
    _deliver(tmp_path, F1)
    src.run(F2, orders="(2, 'B'), (3, 'c')", events="('y', 2)", totals="('all', 20)")
    _deliver(tmp_path, F2)

    con = _con()
    con.execute(f"ATTACH 'ducklake:sqlite:{tmp_path / 'meta.sqlite'}' AS r (READ_ONLY)")
    assert {t for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'r'").fetchall()} == {
        "orders", "events", "totals", "_duckstring_egress"}  # no Trickle companions (orders__changelog)
    con.close()
    tables, snaps, marks = _read(tmp_path)
    assert tables == {
        "orders": [(2, "B"), (3, "c")],  # 1 removed, 2 updated, 3 added: applied by key
        "events": [("x", 1), ("y", 2)],  # appended
        "totals": [("all", 20)],  # replaced whole
    }
    assert [(a, m) for a, m, _ in snaps] == [("duckstring", "sales@1"), ("duckstring", "sales@1")]
    assert json.loads(snaps[-1][2]) == {"pond": "sales", "major": 1, "f": F2.isoformat()}
    assert set(marks.values()) == {F2.isoformat()}

    # Delivering the same run again changes nothing and adds no snapshot (exactly-once).
    _deliver(tmp_path, F2)
    assert _read(tmp_path) == (tables, snaps, marks)


def test_a_resync_replaces_what_was_changed_by_hand(tmp_path):
    src = _Source(tmp_path)
    src.run(F1, orders="(1, 'a'), (2, 'b')", events="('x', 1)", totals="('all', 10)")
    _deliver(tmp_path, F1)
    delivered = _read(tmp_path)[0]
    con = _con()
    con.execute(f"ATTACH 'ducklake:sqlite:{tmp_path / 'meta.sqlite'}' AS w")
    con.execute("DELETE FROM w.main.orders WHERE id = 1")
    con.close()

    _deliver(tmp_path, F1)  # an ordinary delivery finds this run delivered already
    assert _read(tmp_path)[0]["orders"] == [(2, "b")]
    _deliver(tmp_path, F1, resync=True)
    assert _read(tmp_path)[0] == delivered


def test_resync_marks_only_the_next_job(tmp_path):
    from tests.test_egress_postgres import _published_driver

    d = _published_driver(tmp_path, {"totals": {"mode": "overwrite", "f": F1.isoformat()}})
    d.add_spout("sales@1", "lake", "totals", _lake(tmp_path), "auto")
    d.resync_spout("sales@1", "lake")
    d._pending_egress.append(("sales#lake@1", F1))
    assert [j["resync"] for j in d.take_spout_jobs()] == [True]
    d._pending_egress.append(("sales#lake@1", F1))
    assert [j["resync"] for j in d.take_spout_jobs()] == [False]


def test_a_failed_table_rolls_back_the_whole_run(tmp_path, monkeypatch):
    from duckstring.egress.ducklake import DuckLakeEgressDriver

    src = _Source(tmp_path)
    src.run(F1, orders="(1, 'a')", events="('x', 1)", totals="('all', 10)")
    _deliver(tmp_path, F1)
    before = _read(tmp_path)

    src.run(F2, orders="(1, 'A')", events="('y', 2)", totals="('all', 20)")
    real = DuckLakeEgressDriver.write_full

    def failing(self, con, relation, *, table, pk, f):
        if table == "totals":
            raise RuntimeError("the destination went away")
        return real(self, con, relation, table=table, pk=pk, f=f)

    monkeypatch.setattr(DuckLakeEgressDriver, "write_full", failing)
    with pytest.raises(RuntimeError, match="went away"):
        _deliver(tmp_path, F2)
    assert _read(tmp_path) == before  # neither the other tables nor any watermark moved

    monkeypatch.setattr(DuckLakeEgressDriver, "write_full", real)
    _deliver(tmp_path, F2)  # the retry delivers the run whole
    assert _read(tmp_path)[0]["orders"] == [(1, "A")]


def test_a_column_the_source_gains_is_added_to_the_lake_table(tmp_path):
    src = _Source(tmp_path)
    src.con.execute("CREATE TABLE totals AS SELECT 'all' AS k, 10::INTEGER AS total")
    src.publish(F1)
    _deliver(tmp_path, F1, table="totals")
    src.con.execute("CREATE OR REPLACE TABLE totals AS SELECT 'all' AS k, 20::BIGINT AS total, 'eur' AS currency")
    src.publish(F2)
    _deliver(tmp_path, F2, table="totals")

    con = _con()
    con.execute(f"ATTACH 'ducklake:sqlite:{tmp_path / 'meta.sqlite'}' AS r (READ_ONLY)")
    assert dict((c, t) for c, t, *_ in con.execute("DESCRIBE r.main.totals").fetchall()) == {
        "k": "VARCHAR", "total": "BIGINT", "currency": "VARCHAR"}  # INTEGER widened; a column added
    assert con.execute("SELECT * FROM r.main.totals").fetchall() == [("all", 20, "eur")]


def test_a_lake_spout_accepts_a_table_without_a_key(tmp_path):
    from tests.test_egress_postgres import _published_driver

    d = _published_driver(tmp_path, {"snapshot": {"mode": "overwrite", "f": F1.isoformat()}})
    d.add_spout("sales@1", "lake", "snapshot", _lake(tmp_path), "auto")  # refused for Postgres; fine for a lake


def test_the_test_button_attaches_without_delivering(tmp_path):
    get_egress(_lake(tmp_path)).test_connection(_con())
    con = _con()
    con.execute(f"ATTACH 'ducklake:sqlite:{tmp_path / 'meta.sqlite'}' AS r (READ_ONLY)")
    assert con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = 'r'").fetchall() == [(0,)]


# ─── A lake with its files in S3, and its catalog in Postgres when CI provides one ──


@pytest.fixture
def s3_lake(tmp_path):
    """``(destination, data_path)``: a lake whose files are in S3 (``DUCKSTRING_TEST_S3``, MinIO in CI, else an
    in-process moto) and whose catalog is in Postgres (``DUCKSTRING_TEST_PG``) or, without one, SQLite."""
    import os
    import uuid
    from urllib.parse import urlsplit

    from tests.test_object_store import _KEY, _SECRET, _free_port

    boto3 = pytest.importorskip("boto3")
    server, endpoint = None, os.environ.get("DUCKSTRING_TEST_S3", "").rstrip("/")
    if not endpoint:
        from moto.server import ThreadedMotoServer

        port = _free_port()
        server = ThreadedMotoServer(port=port, verbose=False)
        server.start()
        endpoint = f"http://127.0.0.1:{port}"
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                      aws_access_key_id=_KEY, aws_secret_access_key=_SECRET)
    try:
        s3.create_bucket(Bucket="duckstring-lake")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    run = uuid.uuid4().hex[:8]
    if os.environ.get("DUCKSTRING_TEST_PG"):
        u = urlsplit(os.environ["DUCKSTRING_TEST_PG"])
        catalog = (f"postgres:dbname={u.path.lstrip('/')} host={u.hostname} port={u.port or 5432} "
                   f"user={u.username} password={u.password}")
    else:
        catalog = f"sqlite:{tmp_path / 'meta.sqlite'}"
    data_path = f"s3://duckstring-lake/{run}/"
    dest = (f"ducklake:{catalog}?data_path={data_path}&schema=s_{run}&region=us-east-1"
            f"&endpoint={endpoint}&key_id={_KEY}&secret={_SECRET}")
    yield dest, data_path
    if server is not None:
        server.stop()


@pytest.mark.timeout(120)
def test_a_lake_on_object_storage(tmp_path, s3_lake):
    dest, data_path = s3_lake
    src = _Source(tmp_path)
    src.run(F1, orders="(1, 'a'), (2, 'b')", events="('x', 1)", totals="('all', 10)")
    _egress_spout(tmp_path, {"pond_name": "sales", "major": 1, "table": None, "destination": dest, "f": F1.isoformat()})
    # Beyond DuckLake's data inlining (small writes stay in the catalog), so files land in the bucket.
    src.con.execute("CREATE TABLE big AS SELECT range AS i FROM range(5000)")
    src.run(F2, orders="(2, 'B'), (3, 'c')", events="('y', 2)", totals="('all', 20)")
    _egress_spout(tmp_path, {"pond_name": "sales", "major": 1, "table": None, "destination": dest, "f": F2.isoformat()})

    reader = get_egress(dest)
    con = _con()
    prefix = reader._attach(con)  # a fresh attach, with the data path's credentials
    assert {t: sorted(con.execute(f'SELECT * FROM {prefix}."{t}"').fetchall()) for t in ("orders", "events", "totals")} == {
        "orders": [(2, "B"), (3, "c")], "events": [("x", 1), ("y", 2)], "totals": [("all", 20)]}
    assert con.execute(f'SELECT count(*), sum(i) FROM {prefix}.big').fetchall() == [(5000, 12497500)]
    files = con.execute(f"SELECT count(*) FROM glob('{data_path}**/*.parquet')").fetchall()[0][0]
    assert files > 0  # the data really is in the bucket
    get_egress(dest).test_connection(_con())  # the Test button: attach and list the data path
