"""The Flock's SQL-engine machinery, offline (plans/flock-coverage.md, parts 1 to 4).

A SQL engine gets the job translated by sqlglot, its sources staged as Parquet, and its result read back,
restored and verified against a local recompute of a sample of keys. Here DuckDB itself stands in for the
engine (the translation is generated for DuckDB's dialect, so it must run there and match), which tests the
translation, staging, restore and verification paths without a server. What StarRocks and Athena are trusted
with is measured against the real engines in their own conformance tests.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from duckstring import agg, flock
from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.flock import translate
from duckstring.flock.engines.sql_engine import SqlEngine
from duckstring.flock.engines.starrocks import PROFILE as STARROCKS
from duckstring.flock.engines.starrocks import StarRocksEngine

pytest.importorskip("sqlglot")
pytestmark = pytest.mark.timeout(30)

UTC = timezone.utc
EVERYTHING = translate.Profile(dialect="duckdb", allowed=frozenset())


class DuckDBAsSqlEngine(SqlEngine):
    """DuckDB playing a SQL engine: translated SQL, staged Parquet in a local scratch directory, result files."""

    PROFILE = EVERYTHING

    def __init__(self, scratch, *, distort=None, accept=()):
        super().__init__({}, name="duckdb-sql")
        self.scratch = str(scratch)
        self.measuring = True  # the stand-in allows everything; profiles are tested separately below
        self.accept = accept
        self.distort = distort
        self.executed: list[str] = []

    def _setting(self, key):
        return None

    def read_of(self, uri, columns, tag, i):
        return f"read_parquet('{uri}/data.parquet')"

    def execute(self, sql, out_uri, tag):
        import os

        self.executed.append(sql)
        os.makedirs(out_uri, exist_ok=True)
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        query = f"SELECT * FROM ({sql}) AS q"
        if self.distort:
            query = self.distort(query)
        con.execute(f"COPY ({query}) TO '{out_uri}data.parquet' (FORMAT PARQUET)")
        return {}


@pytest.fixture(autouse=True)
def _reset_stats():
    flock._stats.update({"dispatched": 0, "failed": 0, "last_error": None})
    yield


@pytest.fixture
def pond(tmp_path):
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    con.execute("SET TimeZone = 'UTC'")
    p = Pond("p", "1", con, root=tmp_path, f=datetime(2026, 10, 11, tzinfo=UTC), flock="off")
    T.merge_table(con, "orders", con.sql(
        "SELECT range AS order_id, 'P' || (range % 6) AS Product, (range % 4) + 1 AS qty, "
        "CAST(range * 1.25 AS DECIMAL(12,2)) AS amount, CAST(range AS HUGEINT) * 100000000000 AS big, "
        "TIMESTAMPTZ '2026-01-01 00:00:00+00' + range * INTERVAL 7 HOUR AS placed, range % 3 = 0 AS rush "
        "FROM range(300)"), p.f, ("order_id",))
    T.merge_table(con, "product", con.sql(
        "SELECT 'P' || range AS Product, 'Name ' || range AS name, CAST(range + 0.5 AS DOUBLE) AS weight "
        "FROM range(5)"), p.f, ("Product",))
    return p


def _run(pond, engine, build, kind="rows", pk=("order_id",)):
    from trickle.builder import ComprehensiveJob

    job = ComprehensiveJob(build(pond), pk, kind)
    local = sorted(job.relation().fetchall(), key=repr)
    result = flock._dispatch(engine, job)
    return job, local, (sorted(result.fetchall(), key=repr) if result is not None else None)


def _o(p):
    return p.trickle("orders")


def _pr(p):
    return p.trickle("product")


SHAPES = {
    "star": lambda p: _o(p),
    "join_mutate": lambda p: (_o(p).join(_pr(p), on="Product", how="left")
                              .mutate(total="s0.qty * s0.amount", per="s0.amount / s0.qty",
                                      heavy="coalesce(s1.weight, 0) > 2")
                              .filter("total > 10").select("s0.order_id, s1.name, total, per, heavy, s0.placed")),
    "replace_column": lambda p: _o(p).mutate(qty="s0.qty * 10").select("s0.order_id, qty, s0.big"),
    "full": lambda p: (_o(p).join(_pr(p), on="Product", how="full")
                       .select("s0.order_id, s1.Product AS p2, s0.amount")),
    "anti": lambda p: _o(p).join(_pr(p), on="Product", how="anti"),
    "sql_chain": lambda p: (_o(p).alias("o").sql("SELECT order_id, Product, amount, "
                                                 "row_number() OVER (PARTITION BY Product ORDER BY amount DESC) AS rn "
                                                 "FROM o").alias("r").sql("SELECT order_id, rn FROM r WHERE rn <= 3")),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_translated_jobs_run_on_the_stand_in_and_match(pond, tmp_path, shape):
    engine = DuckDBAsSqlEngine(tmp_path / "scratch")
    pk = ("order_id",)
    if shape == "full":
        pond.con.execute("SELECT 1")  # (pk can be NULL for right-only rows; compare whole rows instead)
    job, local, remote = _run(pond, engine, SHAPES[shape], pk=pk)
    assert remote is not None, flock.stats()
    assert local and local == remote
    assert '"__dsflock_leaf' not in engine.executed[-1]  # every source was substituted


def test_types_survive_the_trip(pond, tmp_path):
    """TIMESTAMPTZ travels as UTC wall-clock time and HUGEINT as DECIMAL(38,0); both come back as they were."""
    engine = DuckDBAsSqlEngine(tmp_path / "scratch")
    job, local, remote = _run(pond, engine, lambda p: _o(p).select("s0.order_id, s0.placed, s0.big"))
    assert local == remote
    assert job.schema() == {"order_id": "BIGINT", "placed": "TIMESTAMP WITH TIME ZONE", "big": "HUGEINT"}


def test_an_aggregate_comes_back_as_its_accumulators_with_the_stamp_added_locally(pond, tmp_path):
    engine = DuckDBAsSqlEngine(tmp_path / "scratch", accept=("float_order",))
    job, local, remote = _run(pond, engine, lambda p: _o(p).aggregate(
        "Product", n=agg.count(), s=agg.sum("amount"), v=agg.var("amount"), lo=agg.min("placed")),
        kind="aggregate", pk=("Product",))
    assert remote is not None and len(local) == 6
    for a, b in zip(local, remote, strict=True):
        assert a == pytest.approx(b, rel=1e-12)
    assert "_duckstring_f" not in engine.executed[-1]  # the constant stamp is never sent


def test_mixed_case_column_names_keep_duckdbs_spelling(pond, tmp_path):
    engine = DuckDBAsSqlEngine(tmp_path / "scratch")
    job, local, remote = _run(pond, engine, lambda p: _o(p).select("s0.order_id, s0.Product"))
    assert local == remote and list(job.schema()) == ["order_id", "Product"]


# ── sampled verification ─────────────────────────────────────────────────────────


def test_a_column_wrong_on_every_row_is_caught(pond, tmp_path):
    engine = DuckDBAsSqlEngine(tmp_path / "scratch",
                               distort=lambda q: f"SELECT * REPLACE (amount + 0.01 AS amount) FROM ({q})")
    _job, _local, remote = _run(pond, engine, lambda p: _o(p).select("s0.order_id, s0.amount"))
    assert remote is None
    s = flock.stats()
    assert s["failed"] == 1 and "sampled keys differ" in s["last_error"]


def test_missing_rows_are_caught(pond, tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_FLOCK_VERIFY_KEYS", "100000")  # sample every key, so the dropped one is in it
    engine = DuckDBAsSqlEngine(tmp_path / "scratch", distort=lambda q: f"SELECT * FROM ({q}) WHERE order_id <> 17")
    _job, _local, remote = _run(pond, engine, lambda p: _o(p).select("s0.order_id, s0.amount"))
    assert remote is None and "differ" in flock.stats()["last_error"]


def test_a_correct_result_passes_verification_and_an_untraceable_key_is_skipped(pond, tmp_path):
    engine = DuckDBAsSqlEngine(tmp_path / "scratch")
    *_, remote = _run(pond, engine, lambda p: _o(p).mutate(k="s0.order_id * 2").select("k, s0.amount"), pk=("k",))
    assert remote is not None and flock.stats()["failed"] == 0


def test_verification_can_be_turned_off(pond, tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_FLOCK_VERIFY_KEYS", "0")
    engine = DuckDBAsSqlEngine(tmp_path / "scratch",
                               distort=lambda q: f"SELECT * REPLACE (amount + 0.01 AS amount) FROM ({q})")
    *_, remote = _run(pond, engine, lambda p: _o(p).select("s0.order_id, s0.amount"))
    assert remote is not None


# ── what StarRocks is sent (the profile, without a server) ──────────────────────


def _reason(pond, build, kind="rows", pk=("order_id",), accept=()):
    from trickle.builder import ComprehensiveJob

    return translate.translate(ComprehensiveJob(build(pond), pk, kind), STARROCKS, accept,
                               drop_columns=("_duckstring_f",) if kind == "aggregate" else ()).reason


def test_starrocks_takes_every_join_and_the_builders_own_sql(pond):
    for shape in ("star", "join_mutate", "replace_column", "full", "anti", "sql_chain"):
        assert _reason(pond, SHAPES[shape]) is None, shape


def test_starrocks_refuses_what_was_measured_to_differ(pond):
    assert "Upper" in _reason(pond, lambda p: _o(p).select("s0.order_id, upper(s0.Product) AS u"))
    assert "string" in _reason(pond, lambda p: _o(p).select("s0.order_id, CAST(s0.Product AS INTEGER) AS u"))
    assert "floating point" in _reason(pond, lambda p: _pr(p).select("s0.Product, CAST(s0.weight AS VARCHAR) AS w"),
                                       pk=("Product",))
    assert "measured" in _reason(pond, lambda p: _o(p).alias("o").sql("SELECT order_id, median(amount) AS m FROM o "
                                                                      "GROUP BY order_id"))


def test_starrocks_needs_float_order_for_floating_point_statistics(pond):
    def build(p):
        return _o(p).aggregate("Product", v=agg.var("amount"))

    assert "float_order" in _reason(pond, build, "aggregate", ("Product",))
    assert _reason(pond, build, "aggregate", ("Product",), accept=("float_order",)) is None


def test_starrocks_rewrites_division_and_integer_casts(pond):
    from trickle.builder import ComprehensiveJob

    job = ComprehensiveJob(_o(pond).select("s0.order_id, s0.amount / s0.qty AS per, CAST(s0.amount AS INTEGER) AS a"),
                           ("order_id",))
    sql = translate.translate(job, STARROCKS, ()).sql
    assert "ASSERT_TRUE" in sql.upper() and "/ CAST(" in sql and "ROUND(" in sql.upper()


def test_time_zone_dependent_functions_need_a_utc_duck(pond):
    pond.con.execute("SET TimeZone = 'Australia/Melbourne'")
    assert "time zone" in _reason(pond, lambda p: _o(p).select("s0.order_id, extract(hour FROM s0.placed) AS h"))
    assert _reason(pond, lambda p: _o(p).select("s0.order_id, s0.placed")) is None  # passing it through is fine
    pond.con.execute("SET TimeZone = 'UTC'")
    assert _reason(pond, lambda p: _o(p).select("s0.order_id, extract(hour FROM s0.placed) AS h")) is None


def test_the_starrocks_engine_sends_the_translated_job(pond, tmp_path, monkeypatch):
    """The engine's own path with the server replaced: what it sends, and that it runs the job's sources
    through FILES() and writes the result with INSERT INTO FILES."""
    import sys
    import types

    sent: list[str] = []

    class Cursor:
        rowcount = 0

        def execute(self, sql):
            sent.append(sql)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pymysql", types.SimpleNamespace(connect=lambda **kw: Conn()))
    engine = StarRocksEngine({"DUCKSTRING_FLOCK_STARROCKS_HOST": "sr", "DUCKSTRING_FLOCK_STARROCKS_USER": "u",
                              "DUCKSTRING_FLOCK_STARROCKS_SCRATCH": str(tmp_path / "scratch"),
                              "DUCKSTRING_FLOCK_STARROCKS_WAREHOUSE": "flock", "STARROCKS_PASSWORD": "pw"})
    from trickle.builder import ComprehensiveJob

    job = ComprehensiveJob(_o(pond).join(_pr(pond), on="Product").select("s0.order_id, s1.name"), ("order_id",))
    assert engine.enabled() and engine.eligible(job) is None
    assert engine.dispatch(job) is None  # nothing wrote a result: the dispatch fails and the job runs locally
    assert sent[2] == 'SET warehouse = "flock"'
    insert = sent[-1]
    assert insert.startswith("INSERT INTO FILES(") and insert.count("FROM FILES(") == 2
    assert '"aws.s3.use_instance_profile" = "true"' in insert


def test_starrocks_is_in_the_default_order_after_motherduck():
    sr = {"DUCKSTRING_FLOCK_STARROCKS_HOST": "h", "DUCKSTRING_FLOCK_STARROCKS_USER": "u",
          "DUCKSTRING_FLOCK_STARROCKS_SCRATCH": "s3://b/s"}
    athena = {"DUCKSTRING_FLOCK_ATHENA_WORKGROUP": "wg", "DUCKSTRING_FLOCK_ATHENA_DATABASE": "db",
              "DUCKSTRING_FLOCK_ATHENA_SCRATCH": "s3://b/s"}
    assert flock.default_engine({**sr, **athena}) == "starrocks"
    assert flock.default_engine({**sr, "MOTHERDUCK_TOKEN": "t"}) == "motherduck"
    assert flock.engine_secrets("starrocks") == ("STARROCKS_PASSWORD", "STARROCKS_S3_ACCESS_KEY",
                                                 "STARROCKS_S3_SECRET_KEY")
