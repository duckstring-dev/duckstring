"""What a builder Run reads and materialises (plans/trickle-insert-only.md, Part 1).

The results are covered by the recompute gates (packages/trickle/tests/test_trickle_join_delta.py, the
aggregate suites); these check that a Run does no work its result doesn't need: a Source's current state
that no term reads, the delta under a one-pass join, a chained step's change when nothing chains.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from duckstring import agg
from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane
from duckstring.engine.core import NEVER

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


def _producer(tmp_path, name):
    con = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
    return con, tmp_path / "ponds" / name / "m1" / "data"


def _publish(con, data_dir, f):
    ParquetDataPlane().export(con, data_dir, f=f)


def _rows(con, data_dir, table, proj="*"):
    return sorted(con.sql(f"SELECT {proj} FROM ({ParquetDataPlane().read_select(data_dir, table)})").fetchall())


def _sales(tmp_path):
    con, data = _producer(tmp_path, "sales")

    def run(state, f):
        vals = ", ".join(f"({o}, '{p}', {q})" for o, p, q in state)
        T.merge_table(con, "line", con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, qty)"), f,
                      ("order_id",))
        _publish(con, data, f)

    return run


def _catalog(tmp_path):
    con, data = _producer(tmp_path, "catalog")

    def run(state, f):
        vals = ", ".join(f"('{p}', '{c}')" for p, c in state)
        T.merge_table(con, "product", con.sql(f"SELECT * FROM (VALUES {vals}) v(product_id, category)"), f,
                      ("product_id",))
        _publish(con, data, f)

    return run


def _spy(monkeypatch, cls, name):
    calls = []
    orig = getattr(cls, name)

    def spy(self, *a, **k):
        calls.append(a[0] if a else None)
        return orig(self, *a, **k)

    monkeypatch.setattr(cls, name, spy)
    return calls


def test_an_aggregate_never_reads_its_sources_current_state_when_nothing_rescans(tmp_path, monkeypatch):
    sales = _sales(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    out = tmp_path / "ponds" / "units" / "m1" / "data"

    def run(f, pf):
        pond = Pond("units", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1}, f=f, previous_f=pf)
        (pond.trickle("sales.line", p=1.0)
             .aggregate(by="product_id", units=agg.sum("qty"), n=agg.count(), biggest=agg.max("qty"))
             .merge("units"))
        _publish(snk, out, f)

    sales([(1, "a", 2), (2, "b", 1)], ts(1))
    run(ts(1), NEVER)
    reads = _spy(monkeypatch, Pond, "read_table")
    sales([(1, "a", 2), (2, "b", 1), (3, "a", 5)], ts(2))  # an insert: extends max, no rescan
    run(ts(2), ts(1))
    assert "sales.line" not in reads
    assert _rows(snk, out, "units", "product_id, units, n, biggest") == [("a", 7, 2, 5), ("b", 1, 1, 1)]

    reads.clear()
    sales([(1, "a", 2), (2, "b", 1)], ts(3))  # the max's row retracted: a rescan, which reads it
    run(ts(3), ts(2))
    assert "sales.line" in reads
    assert _rows(snk, out, "units", "product_id, units, n, biggest") == [("a", 2, 1, 2), ("b", 1, 1, 1)]


def test_a_left_join_to_an_unchanged_side_is_one_pass(tmp_path, monkeypatch):
    from trickle.builder import TrickleBuilder

    sales, catalog = _sales(tmp_path), _catalog(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    out = tmp_path / "ponds" / "lines" / "m1" / "data"

    def run(f, pf):
        pond = Pond("lines", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=f,
                    previous_f=pf)
        (pond.trickle("sales.line", p=1.0).alias("l")
             .join(pond.trickle("catalog.product", p=1.0).alias("p"), on="product_id", how="left")
             .join(pond.trickle("catalog.product", p=1.0).alias("q"), on={"l.product_id": "product_id"}, how="left")
             .select("l.order_id, l.qty, p.category, q.category AS category2")
             .merge("lines", pk="order_id"))
        _publish(snk, out, f)

    catalog([("a", "x"), ("b", "y")], ts(1))
    sales([(1, "a", 2), (2, "b", 1)], ts(1))
    run(ts(1), NEVER)
    scratch = _spy(monkeypatch, TrickleBuilder, "_scratch_table")
    sales([(1, "a", 2), (2, "b", 1), (3, "c", 4)], ts(2))  # only the left side changes
    run(ts(2), ts(1))
    assert scratch == []
    assert _rows(snk, out, "lines", "order_id, category, category2") == [
        (1, "x", "x"), (2, "y", "y"), (3, None, None)]

    catalog([("a", "x"), ("b", "z"), ("c", "w")], ts(3))  # now the right side changes: the general terms
    run(ts(3), ts(2))
    assert _rows(snk, out, "lines", "order_id, category, category2") == [
        (1, "x", "x"), (2, "z", "z"), (3, "w", "w")]


def test_a_terminal_prepares_a_chained_change_only_when_something_chains(tmp_path, monkeypatch):
    sales = _sales(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    out = tmp_path / "ponds" / "copy" / "m1" / "data"

    def run(f, pf, chain):
        pond = Pond("copy", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1}, f=f, previous_f=pf)
        b = pond.trickle("sales.line", p=1.0).select("s0.order_id, s0.qty").merge("copy", pk="order_id")
        if chain:
            b.aggregate(by="qty", n=agg.count()).merge("by_qty")
        _publish(snk, out, f)

    sales([(1, "a", 2)], ts(1))
    run(ts(1), NEVER, chain=True)
    deltas = []
    orig = T.read_registry_delta

    def spy(*a, **k):
        deltas.append(a[1] if len(a) > 1 else k.get("table"))
        return orig(*a, **k)

    import trickle.builder as builder

    monkeypatch.setattr(builder, "read_registry_delta", spy)
    sales([(1, "a", 2), (2, "b", 2)], ts(2))
    run(ts(2), ts(1), chain=True)
    assert deltas == ["copy"]
    assert _rows(snk, out, "by_qty", "qty, n") == [(2, 2)]
    deltas.clear()
    sales([(1, "a", 2), (2, "b", 2), (3, "c", 1)], ts(3))
    run(ts(3), ts(2), chain=False)
    assert deltas == []
