"""The remote-DuckDB Flock engines (MotherDuck; plans/flock-motherduck.md, plans/flock-coverage.md).

The job a terminal offers is one self-contained DuckDB query, so a DuckDB engine elsewhere runs it
unchanged and every builder shape is eligible. These tests run that path against a **separate** DuckDB
instance standing in for the remote one: it can't see the Duck's views or registry, so a query that leaned
on anything local would fail here exactly as it would on MotherDuck. Each shape is computed both ways and
compared row for row."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from duckstring import acc, agg, flock
from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane
from duckstring.engine.core import NEVER
from duckstring.flock.engines.motherduck import MotherDuckEngine
from duckstring.flock.engines.remote_duckdb import RemoteDuckDBEngine

pytestmark = pytest.mark.timeout(30)

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


class StandIn(RemoteDuckDBEngine):
    """A remote DuckDB that is a fresh in-process instance. ``in_place`` lets it read published Sources from
    this machine's files (the stand-in for an object-store data root); otherwise every source is uploaded."""

    def __init__(self, env=None, *, in_place=True, fail=False):
        super().__init__(env or {}, name="stand-in")
        self.reads_local_paths = in_place
        self.fail = fail
        self.calls = 0

    def connect(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("stand-in: unreachable")
        return duckdb.connect()


@pytest.fixture(autouse=True)
def _reset_stats():
    flock._stats.update({"dispatched": 0, "failed": 0, "last_error": None})
    yield


def _publish(con, data_dir, f):
    ParquetDataPlane().export(con, data_dir, f=f)


@pytest.fixture
def sources(tmp_path):
    return publish_sources(tmp_path)


def publish_sources(tmp_path):
    """Two published Sources: ``sales.order_line`` (a merge Trickle) and ``catalog.product`` (a plain table),
    plus a third, ``catalog.region``, for bushy joins."""
    sales = duckdb.connect(str(tmp_path / "sales.duckdb"))
    catalog = duckdb.connect(str(tmp_path / "catalog.duckdb"))
    T.merge_table(sales, "order_line", sales.sql(
        "SELECT range AS order_id, 'p' || (range % 9) AS product_id, (range % 5) + 1 AS qty, "
        "CAST(range % 3 AS VARCHAR) AS region_id, (range % 4 = 0) AS rush FROM range(400)"), ts(1), ("order_id",))
    catalog.execute("CREATE TABLE product AS SELECT 'p' || range AS product_id, "
                    "CAST(range * 2.5 + 1 AS DECIMAL(10,2)) AS price, 'Prod ' || range AS name, "
                    "CAST(range % 3 AS VARCHAR) AS region_id FROM range(8)")  # p8 has no product
    catalog.execute("CREATE TABLE region AS SELECT CAST(range AS VARCHAR) AS region_id, "
                    "'R' || range AS region_name FROM range(2)")  # region 2 has none
    _publish(sales, tmp_path / "ponds" / "sales" / "m1" / "data", ts(1))
    _publish(catalog, tmp_path / "ponds" / "catalog" / "m1" / "data", ts(1))
    return tmp_path


def _pond(root, f=ts(2), previous_f=NEVER, mode="off"):
    con = duckdb.connect(str(root / "snk.duckdb"))
    return Pond("snk", "1.0.0", con, root=root, source_majors={"sales": 1, "catalog": 1},
                source_f={"sales": ts(1).isoformat(), "catalog": ts(1).isoformat()},
                f=f, previous_f=previous_f, flock=mode)


@pytest.fixture
def engine(monkeypatch):
    eng = StandIn()
    monkeypatch.setattr(flock, "get_engine", lambda env=None: eng)
    monkeypatch.setattr(flock, "enabled", lambda env=None: True)
    return eng


def _current(con, table):
    return sorted(T.current_state(con, table).fetchall(), key=repr)


def _both(pond, build, name, pk):
    """Run ``build`` locally (Flock off) and through the engine (``always``); return both outputs."""
    pond.flock = "off"
    build(pond).merge(f"{name}_local", pk=pk)
    pond.flock = "always"
    before = flock.stats() or {"dispatched": 0}
    build(pond).merge(f"{name}_remote", pk=pk)
    after = flock.stats()
    assert after["failed"] == 0, after["last_error"]
    assert after["dispatched"] == before["dispatched"] + 1
    return _current(pond.con, f"{name}_local"), _current(pond.con, f"{name}_remote")


def _ol(p):
    return p.trickle("sales.order_line")


def _pr(p):
    return p.trickle("catalog.product")


SHAPES = {
    "inner_select_arith": (lambda p: _ol(p).join(_pr(p), on="product_id")
                           .select("s0.order_id, s1.name, s0.qty * s1.price AS total"), "order_id"),
    "left_mutate_filter": (lambda p: _ol(p).join(_pr(p), on="product_id", how="left")
                           .mutate(total="s0.qty * coalesce(s1.price, 0)", each="s0.qty / 3")
                           .filter("total >= 0 AND NOT s0.rush")
                           .select("s0.order_id, total, each, upper(s1.name) AS uname"), "order_id"),
    "full_join_star": (lambda p: _ol(p).alias("o").join(_pr(p).alias("pr"), on="product_id", how="full")
                       .mutate(key="coalesce(CAST(o.order_id AS VARCHAR), 'none-' || pr.product_id)")
                       .select("key, o.qty, pr.price"), "key"),
    "semi": (lambda p: _ol(p).join(_pr(p), on="product_id", how="semi"), "order_id"),
    "anti": (lambda p: _ol(p).join(_pr(p), on="product_id", how="anti"), "order_id"),
    "bushy": (lambda p: _ol(p).join(_pr(p).join(p.trickle("catalog.region"), on="region_id", how="left"),
                                    on="product_id")
              .select("s0.order_id, s1.name, s2.region_name, CAST(s0.qty AS DOUBLE) / 7 AS frac"), "order_id"),
    "chained_selects": (lambda p: _ol(p).select("s0.order_id, s0.qty").mutate(q2="qty * qty")
                        .filter("q2 > 4").select("order_id, q2"), "order_id"),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_shape_runs_remotely_and_matches_local(sources, engine, shape):
    build, pk = SHAPES[shape]
    local, remote = _both(_pond(sources), build, shape, pk)
    assert local and local == remote


def test_aggregate_ships_its_accumulators_and_matches_local(sources, engine):
    def build(p):
        return (_ol(p).join(_pr(p), on="product_id")
                .select("s0.order_id, s0.product_id, s0.rush, s0.qty * s1.price AS total, CAST(s0.qty AS DOUBLE) AS w")
                .aggregate("product_id", n=agg.count(), s=agg.sum("total"), m=agg.mean("total"),
                           v=agg.var("total"), sd=agg.stddev("total", how="pop"), lo=agg.min("total"),
                           hi=agg.max("total"), cov=agg.covariance("total", "w"),
                           r=agg.pearson_correlation("total", "w"), slope=agg.ols_slope("total", "w"),
                           wavg=agg.weighted_average("total", "w"), first=agg.argmin("total", "order_id"),
                           anyrush=agg.bool_or("rush"), prod=agg.product("w")))

    pond = _pond(sources)
    pond.flock = "off"
    build(pond).merge("agg_local")
    pond.flock = "always"
    build(pond).merge("agg_remote")
    assert flock.stats() == {"dispatched": 1, "failed": 0, "last_error": None}
    local, remote = _current(pond.con, "agg_local"), _current(pond.con, "agg_remote")
    assert len(local) == 8
    for lr, rr in zip(local, remote, strict=True):
        for a, b in zip(lr, rr, strict=True):
            assert a == pytest.approx(b, rel=1e-12, nan_ok=True) if isinstance(a, float) else a == b


def test_aggregate_stays_incremental_after_a_remote_bootstrap(sources, engine):
    """The installed accumulators seed the next incremental run like a local rebuild would."""
    pond = _pond(sources, mode="always")
    _ol(pond).aggregate("product_id", n=agg.count(), q=agg.sum("qty")).merge("agg")
    sales = duckdb.connect(str(sources / "sales.duckdb"))
    T.merge_table(sales, "order_line", sales.sql(
        "SELECT range AS order_id, 'p' || (range % 9) AS product_id, (range % 5) + 1 AS qty, "
        "CAST(range % 3 AS VARCHAR) AS region_id, (range % 4 = 0) AS rush FROM range(401)"), ts(3), ("order_id",))
    _publish(sales, sources / "ponds" / "sales" / "m1" / "data", ts(3))
    pond.con.close()
    nxt = _pond(sources, f=ts(4), previous_f=ts(2), mode="always")
    nxt.source_f = {"sales": ts(3).isoformat(), "catalog": ts(1).isoformat()}
    _ol(nxt).aggregate("product_id", n=agg.count(), q=agg.sum("qty")).merge("agg")
    assert engine.calls == 1  # one new row is incremental: never offered
    got = dict((r[0], r[1:]) for r in _current(nxt.con, "agg"))
    want = nxt.con.sql("SELECT product_id, count(*), sum(qty) FROM range(401) r(i), "
                       "LATERAL (SELECT 'p' || (i % 9) AS product_id, (i % 5) + 1 AS qty) GROUP BY 1").fetchall()
    assert got == {r[0]: r[1:] for r in want}


def test_sql_queries_run_remotely_including_window_functions_and_chains(sources, engine):
    def build(p):
        return (_ol(p).join(_pr(p), on="product_id").select("s0.order_id, s0.product_id, s0.qty * s1.price AS total")
                .alias("t")
                .sql("SELECT order_id, total, rank() OVER (PARTITION BY product_id ORDER BY total, order_id) AS rk, "
                     "median(total) OVER (PARTITION BY product_id) AS med FROM t;")
                .alias("u")
                .sql("WITH top AS (SELECT * FROM u WHERE rk <= 10) SELECT order_id, rk, med FROM top"))

    local, remote = _both(_pond(sources), build, "sql", "order_id")
    assert local and local == remote


def test_a_sql_query_reading_another_table_sends_it(sources, engine):
    pond = _pond(sources)
    pond.con.execute("CREATE TEMP TABLE bands AS SELECT * FROM (VALUES (1, 'low'), (5, 'high')) v(qty, band)")

    def build(p):
        return _ol(p).alias("t").sql("SELECT t.order_id, b.band FROM t JOIN bands b USING (qty)")

    local, remote = _both(pond, build, "extra", "order_id")
    assert local and local == remote


def test_sources_are_uploaded_when_the_engine_cannot_read_them(sources, monkeypatch):
    eng = StandIn(in_place=False)
    monkeypatch.setattr(flock, "get_engine", lambda env=None: eng)
    monkeypatch.setattr(flock, "enabled", lambda env=None: True)
    build, pk = SHAPES["bushy"]
    local, remote = _both(_pond(sources), build, "up", pk)
    assert local and local == remote


def test_published_sources_are_read_in_place(sources, engine, monkeypatch):
    uploads = []
    orig = RemoteDuckDBEngine._upload
    monkeypatch.setattr(RemoteDuckDBEngine, "_upload", lambda self, *a: (uploads.append(a[2]), orig(self, *a)))
    build, pk = SHAPES["inner_select_arith"]
    _both(_pond(sources), build, "inplace", pk)
    assert uploads == []


def test_a_failing_engine_falls_back_to_local_and_is_counted(sources, monkeypatch):
    eng = StandIn(fail=True)
    monkeypatch.setattr(flock, "get_engine", lambda env=None: eng)
    monkeypatch.setattr(flock, "enabled", lambda env=None: True)
    pond = _pond(sources, mode="always")
    _ol(pond).join(_pr(pond), on="product_id").select("s0.order_id, s1.price").merge("o", pk="order_id")
    assert len(_current(pond.con, "o")) == 356  # p8 orders have no product
    s = flock.stats()
    assert s["failed"] == 1 and "unreachable" in s["last_error"]


def test_a_coverage_miss_is_offered_under_upgrade(sources, engine, monkeypatch):
    """An incremental run whose source change is over its ``p`` threshold recomputes wholesale anyway, so it
    is offered; before, only a bootstrap or ``ivm=False`` was."""
    monkeypatch.setenv("DUCKSTRING_FLOCK_MIN_ROWS", "1")  # clearly over: dispatch up front
    pond = _pond(sources, mode="off")
    _ol(pond).select("s0.order_id, s0.qty").merge("o", pk="order_id")
    pond.con.close()
    sales = duckdb.connect(str(sources / "sales.duckdb"))
    T.merge_table(sales, "order_line", sales.sql(
        "SELECT range AS order_id, 'p0' AS product_id, 9 AS qty, '0' AS region_id, false AS rush FROM range(400)"),
        ts(3), ("order_id",))
    _publish(sales, sources / "ponds" / "sales" / "m1" / "data", ts(3))
    nxt = _pond(sources, f=ts(4), previous_f=ts(2), mode="upgrade")
    nxt.source_f = {"sales": ts(3).isoformat(), "catalog": ts(1).isoformat()}
    _ol(nxt).select("s0.order_id, s0.qty").merge("o", pk="order_id")
    assert engine.calls == 1
    assert {r[1] for r in _current(nxt.con, "o")} == {9}


def test_accumulate_and_incremental_runs_are_never_offered(sources, engine):
    pond = _pond(sources, mode="always")
    (_ol(pond).along("order_id").accumulate("product_id", running=acc.sum("qty"))
     .merge("run", pk="order_id"))
    assert engine.calls == 0


# ─── MotherDuck: profiles, secrets and the default engine ────────────────────────


def test_motherduck_profiles_map_to_their_own_token():
    assert MotherDuckEngine.secrets_for("motherduck") == ("MOTHERDUCK_TOKEN",)
    assert MotherDuckEngine.secrets_for("motherduck/giga") == ("MOTHERDUCK_TOKEN_GIGA",)
    assert flock.engine_secrets("motherduck/giga") == ("MOTHERDUCK_TOKEN_GIGA",)
    assert MotherDuckEngine({"MOTHERDUCK_TOKEN_GIGA": "t"}, name="motherduck/giga").enabled()
    assert not MotherDuckEngine({"MOTHERDUCK_TOKEN": "t"}, name="motherduck/giga").enabled()
    # MotherDuck's own variable name works for the default profile only
    assert MotherDuckEngine({"motherduck_token": "t"}).enabled()
    assert not MotherDuckEngine({"motherduck_token": "t"}, name="motherduck/giga").enabled()


ATHENA = {"DUCKSTRING_FLOCK_ATHENA_WORKGROUP": "wg", "DUCKSTRING_FLOCK_ATHENA_DATABASE": "db",
          "DUCKSTRING_FLOCK_ATHENA_SCRATCH": "s3://b/s"}


def test_the_default_engine_is_the_first_configured_in_order():
    assert flock.default_engine({}) is None
    assert flock.default_engine(ATHENA) == "athena"
    assert flock.default_engine({**ATHENA, "MOTHERDUCK_TOKEN": "t"}) == "motherduck"
    assert flock.default_engine(ATHENA, secret=lambda n: "t" if n == "MOTHERDUCK_TOKEN" else None) == "motherduck"
    assert flock.get_engine({**ATHENA, "MOTHERDUCK_TOKEN": "t"}).__class__ is MotherDuckEngine
    assert flock.get_engine({**ATHENA, "MOTHERDUCK_TOKEN": "t", "DUCKSTRING_FLOCK_ENGINE": "athena"}
                            ).__class__.__name__ == "AthenaEngine"


def test_the_catchment_pins_the_default_engine_and_sends_its_token():
    out = flock.job_settings({"flock_mode": "upgrade"}, {**ATHENA},
                             secret=lambda n: "tok" if n == "MOTHERDUCK_TOKEN" else None)
    assert out["DUCKSTRING_FLOCK_ENGINE"] == "motherduck" and out["MOTHERDUCK_TOKEN"] == "tok"
    off = flock.job_settings({"flock_mode": "off"}, {}, secret=lambda n: "tok")
    assert "DUCKSTRING_FLOCK_ENGINE" not in off and "MOTHERDUCK_TOKEN" not in off


def test_the_motherduck_engine_runs_a_job_end_to_end(sources, monkeypatch):
    """The whole engine with the MotherDuck connection replaced by a local DuckDB (MotherDuck's
    ``CREATE DATABASE`` stood in for by an attach)."""
    eng = MotherDuckEngine({"MOTHERDUCK_TOKEN": "tok"})
    monkeypatch.setattr(MotherDuckEngine, "connect", lambda self: duckdb.connect())
    monkeypatch.setattr(MotherDuckEngine, "scratch",
                        lambda self, remote: (remote.execute(f"ATTACH ':memory:' AS {self.database}"),
                                              f"{self.database}.main")[1])
    monkeypatch.setattr(flock, "get_engine", lambda env=None: eng)
    monkeypatch.setattr(flock, "enabled", lambda env=None: True)
    build, pk = SHAPES["left_mutate_filter"]
    local, remote = _both(_pond(sources), build, "md", pk)
    assert local and local == remote
