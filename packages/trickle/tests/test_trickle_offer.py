"""Offering a comprehensive recompute to the host (``dispatch_comprehensive``): what is offered and when, and
that :meth:`ComprehensiveJob.sql` is the whole job as one self-contained query, by running it on a
**separate** DuckDB database that can see none of the builder's views. Sources travel to it as Parquet files
and the result comes back the same way."""

from __future__ import annotations

import duckdb
import pytest
from trickle import agg
from trickle.builder import ComprehensiveJob
from trickle.session import Epoch
from trickle_support import NEVER, rows, star, ts


class Offering(Epoch):
    """A host that runs every offered job on another DuckDB database (or declines, with ``decline``)."""

    def __init__(self, con, f, previous_f=NEVER, *, tmp, decline=False):
        super().__init__(con, f, previous_f)
        self.tmp, self.decline = tmp, decline
        self.offered: list[ComprehensiveJob] = []

    def dispatch_comprehensive(self, job):
        self.offered.append(job)
        if self.decline:
            return None
        other = duckdb.connect()
        reads = {}
        for i, leaf in enumerate(job.leaves()):
            path = self.tmp / f"leaf{len(self.offered)}_{i}.parquet"
            self.con.execute(f"COPY ({self.read_table(leaf.ref).sql_query()}) TO '{path}' (FORMAT PARQUET)")
            cols = ", ".join(f'"{c}"' for c in job.columns(leaf))
            reads[id(leaf)] = f"SELECT {cols} FROM read_parquet('{path}')"
        out = self.tmp / f"out{len(self.offered)}.parquet"
        other.execute(f"COPY ({job.sql(lambda leaf: reads[id(leaf)])}) TO '{out}' (FORMAT PARQUET)")
        other.close()
        # Parquet has no HUGEINT (DuckDB writes it as DOUBLE), so cast back to the job's types, as a real host
        # conforms any engine's result to them.
        casts = ", ".join(f'CAST("{c}" AS {t}) AS "{c}"' for c, t in job.schema().items())
        return self.con.sql(f"SELECT {casts} FROM read_parquet('{out}')")


@pytest.fixture
def sources(db):
    ol, pr = star(db)
    ol([(i, f"p{i % 4}", i % 3 + 1) for i in range(40)], ts(1))
    pr([("p0", 5), ("p1", 9), ("p2", 2)], ts(1))
    return ol, pr


def _priced(t):
    return (t.source("order_line").join(t.source("product"), on="product_id", how="left")
            .mutate(total="s0.qty * coalesce(s1.price, 0)")
            .select("s0.order_id, s0.product_id, total"))


def test_a_bootstrap_is_offered_and_an_incremental_run_is_not(db, sources, tmp_path):
    ol, _ = sources
    t = Offering(db, ts(2), tmp=tmp_path)
    _priced(t).merge("priced", pk="order_id")
    assert [j.kind for j in t.offered] == ["rows"]
    ol([(i, f"p{i % 4}", 7 if i == 3 else i % 3 + 1) for i in range(40)], ts(3))
    t = Offering(db, ts(4), ts(2), tmp=tmp_path)
    _priced(t).merge("priced", pk="order_id")
    assert t.offered == []
    assert rows(db, "priced", "order_id, total") == sorted(
        (i, (7 if i == 3 else i % 3 + 1) * {0: 5, 1: 9, 2: 2}.get(i % 4, 0)) for i in range(40))


def test_a_change_over_the_threshold_is_offered(db, sources, tmp_path):
    ol, _ = sources
    _priced(Offering(db, ts(2), tmp=tmp_path, decline=True)).merge("priced", pk="order_id")
    ol([(i, f"p{i % 4}", 9) for i in range(40)], ts(3))  # every row changed: over p
    t = Offering(db, ts(4), ts(2), tmp=tmp_path)
    _priced(t).merge("priced", pk="order_id")
    assert [j.kind for j in t.offered] == ["rows"]
    assert {r[1] for r in rows(db, "priced", "order_id, total")} == {45, 81, 18, 0}


def test_a_declined_offer_computes_locally(db, sources, tmp_path):
    t = Offering(db, ts(2), tmp=tmp_path, decline=True)
    _priced(t).merge("priced", pk="order_id")
    assert len(t.offered) == 1 and len(rows(db, "priced")) == 40


SHAPES = {
    "left_mutate": lambda t: _priced(t),
    "full": lambda t: (t.source("order_line").alias("o").join(t.source("product").alias("p"), on="product_id",
                                                               how="full")
                       .mutate(k="coalesce(CAST(o.order_id AS VARCHAR), 'x' || p.product_id)")
                       .select("k, o.qty, p.price")),
    "semi_star": lambda t: t.source("order_line").join(t.source("product"), on="product_id", how="semi"),
    "anti_filter": lambda t: (t.source("order_line").join(t.source("product"), on="product_id", how="anti")
                              .filter("s0.qty > 1")),
    "sql_chain": lambda t: (_priced(t).alias("x")
                            .sql("SELECT order_id, total, dense_rank() OVER (ORDER BY total DESC) AS r FROM x")
                            .alias("y").sql("SELECT order_id, r FROM y WHERE r <= 3")),
}
PKS = {"full": "k", "semi_star": "order_id", "anti_filter": "order_id"}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_offered_query_reproduces_the_local_result_on_another_database(db, sources, tmp_path, shape):
    build, pk = SHAPES[shape], PKS.get(shape, "order_id")
    build(Offering(db, ts(2), tmp=tmp_path, decline=True)).merge("local", pk=pk)
    t = Offering(db, ts(2), tmp=tmp_path)
    build(t).merge("remote", pk=pk)
    assert len(t.offered) == 1
    assert rows(db, "local") and rows(db, "local") == rows(db, "remote")


def test_an_aggregate_is_offered_as_its_accumulator_state(db, sources, tmp_path):
    def build(t):
        return (_priced(t).aggregate("product_id", n=agg.count(), s=agg.sum("total"), v=agg.var("total"),
                                     lo=agg.min("total")))

    build(Offering(db, ts(2), tmp=tmp_path, decline=True)).merge("local")
    t = Offering(db, ts(2), tmp=tmp_path)
    build(t).merge("remote")
    assert [j.kind for j in t.offered] == ["aggregate"]
    assert list(t.offered[0].schema())[:3] == ["product_id", "_a_cnt", "_a_sum_0"]
    for table in ("local", "_duckstring_agg_local"):
        for a, b in zip(rows(db, table), rows(db, table.replace("local", "remote")), strict=True):
            # Float accumulators may differ in the last bit; keys compare exactly (pytest 9.2's approx refuses a str).
            assert [x if isinstance(x, str) else pytest.approx(x, rel=1e-12) for x in a] == list(b)


def test_sql_runs_at_the_terminal(db, sources, tmp_path):
    """``.sql()`` is deferred to its terminal, so the host can be offered it; ``.schema()`` binds it without
    running it."""
    b = _priced(at_ := Offering(db, ts(2), tmp=tmp_path)).alias("x").sql("SELECT order_id, total * 2 AS t2 FROM x")
    assert list(b.schema()) == ["order_id", "t2"]
    assert at_.offered == []
    b.merge("doubled", pk="order_id")
    assert len(at_.offered) == 1 and len(rows(db, "doubled")) == 40
