"""Incremental aggregation (``.aggregate``: the distributive and algebraic metrics, extremes with rescans,
the weighted family and co-moments, products, ordered reductions) and order-dependent scans
(``.along().accumulate()``: running values, lag and convolution, custom folds, merge-mode re-folds). Each is
checked across a bootstrap, an incremental insert and a retraction, mostly against DuckDB's own aggregates
over the current state."""

from __future__ import annotations

import math

import pytest
from trickle import acc, agg
from trickle.builder import BuildError
from trickle.io import first_row
from trickle_support import NEVER, T, at, merge, rows, star, ts


def _one(db, sql):
    return first_row(db.sql(sql))


# ─── distributive and algebraic ──────────────────────────────────────────────────


def test_builder_aggregate_distributive_incremental(db):
    ol, pr = star(db)

    def run(f, pf):
        t = at(db, f, pf)
        (t.source("order_line", p=1.0).alias("o")
          .join(t.source("product", p=1.0).alias("p"), on="product_id")
          .select("o.product_id, o.qty * p.price AS revenue, o.qty AS units")
          .aggregate(by="product_id", total_revenue=agg.sum("revenue"), units_sold=agg.sum("units"),
                     n=agg.count(), avg_revenue=agg.mean("revenue"))
          .merge("revenue_by_product"))  # pk defaults to product_id

    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    cols = "product_id, total_revenue, units_sold, n, avg_revenue"
    assert rows(db, "revenue_by_product", cols) == [("p1", 25, 5, 2, 12.5), ("p2", 9, 1, 1, 9.0)]
    assert T.read_meta(db)["revenue_by_product"]["mode"] == "merge"

    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3), (13, "p2", 4)], ts(2))  # a new order for p2
    run(ts(2), ts(1))
    assert rows(db, "revenue_by_product", cols) == [("p1", 25, 5, 2, 12.5), ("p2", 45, 5, 2, 22.5)]
    latest = db.sql('SELECT DISTINCT product_id FROM "revenue_by_product__changelog" WHERE _duckstring_f = '
                    '(SELECT max(_duckstring_f) FROM "revenue_by_product__changelog")').fetchall()
    assert latest == [("p2",)]  # only p2's group changed

    pr([("p1", 10), ("p2", 9)], ts(3))  # a dimension change: only p1's group recomputes
    run(ts(3), ts(2))
    assert rows(db, "revenue_by_product", "product_id, total_revenue") == [("p1", 50), ("p2", 45)]


def test_builder_aggregate_group_emptied(db):
    ol, pr = star(db)

    def run(f, pf):
        t = at(db, f, pf)
        (t.source("order_line", p=1.0).alias("o")
          .join(t.source("product", p=1.0).alias("p"), on="product_id")
          .select("o.product_id, o.qty * p.price AS revenue")
          .group_by("product_id").aggregate(total=agg.sum("revenue"), n=agg.count())
          .merge("rev"))

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "rev", "product_id, total, n") == [("p1", 10, 1), ("p2", 9, 1)]
    ol([(10, "p1", 2)], ts(2))  # p2's only order removed: its group empties and is retracted
    run(ts(2), ts(1))
    assert rows(db, "rev", "product_id, total, n") == [("p1", 10, 1)]


def test_agg_count_metric_is_incremental(db):
    def run(f, pf):
        at(db, f, pf).source("fact").aggregate(by="k", n=agg.count()).merge("by_k", pk="k")

    merge(db, "fact", [(1, "A"), (2, "A"), (3, "B")], "id, k", ts(1), "id")
    run(ts(1), NEVER)
    assert rows(db, "by_k", "k, n") == [("A", 2), ("B", 1)]
    merge(db, "fact", [(1, "A"), (3, "B"), (4, "B"), (5, "B")], "id, k", ts(2), "id")
    run(ts(2), ts(1))
    assert rows(db, "by_k", "k, n") == [("A", 1), ("B", 3)]


def test_builder_aggregate_min_max_extend_and_rescan(db):
    ol, _pr = star(db)

    def run(f, pf):
        (at(db, f, pf).source("order_line", p=1.0)
         .aggregate(by="product_id", max_qty=agg.max("qty"), min_qty=agg.min("qty"), n=agg.count())
         .merge("ext"))

    ol([(10, "p1", 2), (11, "p1", 5), (12, "p2", 3)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "ext", "product_id, max_qty, min_qty, n") == [("p1", 5, 2, 2), ("p2", 3, 3, 1)]
    ol([(10, "p1", 2), (11, "p1", 5), (12, "p2", 3), (13, "p1", 1)], ts(2))  # an insert extends in place
    run(ts(2), ts(1))
    assert rows(db, "ext", "product_id, max_qty, min_qty, n") == [("p1", 5, 1, 3), ("p2", 3, 3, 1)]
    ol([(10, "p1", 2), (11, "p1", 3), (12, "p2", 3), (13, "p1", 1)], ts(3))  # retract the max: p1 rescans
    run(ts(3), ts(2))
    assert rows(db, "ext", "product_id, max_qty, min_qty, n") == [("p1", 3, 1, 3), ("p2", 3, 3, 1)]


def test_builder_aggregate_var_stddev(db):
    ol, _pr = star(db)

    def run(f, pf):
        (at(db, f, pf).source("order_line", p=1.0)
         .aggregate(by="product_id", v=agg.var("qty"), sd=agg.stddev("qty"), vp=agg.var("qty", how="pop"))
         .merge("stat"))

    ol([(10, "p1", 2), (11, "p1", 4), (12, "p1", 6)], ts(1))
    run(ts(1), NEVER)  # 2, 4, 6: sample variance 4, stddev 2, population variance 8/3
    v, sd, vp = _one(db, "SELECT v, sd, vp FROM stat")
    assert v == pytest.approx(4.0) and sd == pytest.approx(2.0) and vp == pytest.approx(8 / 3)
    ol([(10, "p1", 2), (11, "p1", 4), (12, "p1", 6), (13, "p1", 8)], ts(2))  # 2..8: sample variance 20/3
    run(ts(2), ts(1))
    assert _one(db, "SELECT v FROM stat")[0] == pytest.approx(20 / 3)


def test_builder_aggregate_variance_numerical_stability(db):
    """The centred-moment maintenance stays accurate where ``Σx² − (Σx)²/n`` cancels: a large offset with a
    tiny spread, grown by inserts and then a retraction, matching DuckDB's ``var_samp``."""
    ol, _pr = star(db)
    base = 1_000_000_000

    def run(f, pf):
        at(db, f, pf).source("order_line", p=1.0).aggregate(by="product_id", v=agg.var("qty")).merge("stat")

    def expected(rows_):
        return _one(db, f"SELECT var_samp(q) FROM (VALUES {', '.join(f'({q})' for _i, _p, q in rows_)}) t(q)")[0]

    r1 = [(i, "p1", base + d) for i, d in enumerate([1, 2, 3, 4, 5])]
    ol(r1, ts(1))
    run(ts(1), NEVER)
    assert _one(db, "SELECT v FROM stat")[0] == pytest.approx(expected(r1), rel=1e-6)
    r2 = r1 + [(i, "p1", base + d) for i, d in enumerate([6, 7, 8, 9, 10], start=5)]
    ol(r2, ts(2))
    run(ts(2), ts(1))
    assert _one(db, "SELECT v FROM stat")[0] == pytest.approx(expected(r2), rel=1e-6)
    r3 = [(0, "p1", base + 100)] + r2[1:]  # change one value: the merge-out branch
    ol(r3, ts(3))
    run(ts(3), ts(2))
    assert _one(db, "SELECT v FROM stat")[0] == pytest.approx(expected(r3), rel=1e-6)


def test_builder_aggregate_weighted_and_comoment(db):
    """The weighted family and the paired co-moments across a bootstrap, an insert and a retraction, against
    DuckDB's native aggregates."""

    def facts(rows_, f):
        merge(db, "f", rows_, "id, g, x, y, w", f, "id")

    def run(f, pf):
        (at(db, f, pf).source("f", p=1.0)
         .aggregate(by="g", wt=agg.weight_total("w"), ws=agg.weighted_sum("x", "w"),
                    wa=agg.weighted_average("x", "w"), cov=agg.covariance("x", "y"),
                    cor=agg.pearson_correlation("x", "y"), slope=agg.ols_slope("x", "y"),
                    icpt=agg.ols_intercept("x", "y"))
         .merge("stat"))

    def expected(g):
        return _one(db, f"SELECT sum(w), sum(w*x), sum(w*x)/sum(w), covar_samp(y, x), corr(y, x), "
                        f"regr_slope(y, x), regr_intercept(y, x) FROM f WHERE g = '{g}'")

    def got(g):
        return _one(db, f"SELECT wt, ws, wa, cov, cor, slope, icpt FROM stat WHERE g = '{g}'")

    r1 = [(1, "a", 1.0, 2.0, 1.0), (2, "a", 2.0, 4.0, 2.0), (3, "a", 3.0, 5.0, 1.0),
          (4, "b", 10.0, 1.0, 3.0), (5, "b", 20.0, 3.0, 2.0), (6, "b", 30.0, 8.0, 1.0)]
    facts(r1, ts(1))
    run(ts(1), NEVER)
    assert got("a") == pytest.approx(expected("a"))
    r2 = r1 + [(7, "a", 4.0, 9.0, 2.0)]
    facts(r2, ts(2))
    run(ts(2), ts(1))
    assert got("a") == pytest.approx(expected("a"))
    facts([(2, "a", 2.0, 4.5, 3.0) if r[0] == 2 else r for r in r2], ts(3))  # change row 2: a retraction
    run(ts(3), ts(2))
    assert got("a") == pytest.approx(expected("a")) and got("b") == pytest.approx(expected("b"))


def test_builder_aggregate_argmax_and_semigroup(db):
    """Payload extremes and the boolean and bitwise semigroups, including a retraction of the supporting
    extreme (forcing a rescan), against DuckDB's native aggregates."""

    def facts(rows_, f):
        merge(db, "f", rows_, "id, g, k, lbl, flag, bits", f, "id")

    def run(f, pf):
        (at(db, f, pf).source("f", p=1.0)
         .aggregate(by="g", top=agg.argmax("lbl", "k"), bot=agg.argmin("lbl", "k"), peak=agg.max("k"),
                    anyflag=agg.bool_or("flag"), allflag=agg.bool_and("flag"), orbits=agg.bit_or("bits"))
         .merge("stat"))

    def expected(g):
        return _one(db, f"SELECT arg_max(lbl, k), arg_min(lbl, k), max(k), bool_or(flag), bool_and(flag), "
                        f"bit_or(bits) FROM f WHERE g = '{g}'")

    def got(g):
        return _one(db, f"SELECT top, bot, peak, anyflag, allflag, orbits FROM stat WHERE g = '{g}'")

    r1 = [(1, "a", 10, "lo", False, 1), (2, "a", 30, "hi", True, 2), (3, "a", 20, "mid", False, 4)]
    facts(r1, ts(1))
    run(ts(1), NEVER)
    assert got("a") == expected("a")
    r2 = r1 + [(4, "a", 40, "top", True, 8)]  # a new max extends in place
    facts(r2, ts(2))
    run(ts(2), ts(1))
    assert got("a") == expected("a")
    facts(r2[:-1], ts(3))  # retract the supporting max: the group rescans
    run(ts(3), ts(2))
    assert got("a") == expected("a")


def test_builder_aggregate_product(db):
    """``agg.product`` through retractable log-sum-exp accumulators, against DuckDB's ``product``, across an
    insert and a retraction that flips the sign."""
    ol, _pr = star(db)

    def run(f, pf):
        at(db, f, pf).source("order_line", p=1.0).aggregate(by="product_id", p=agg.product("qty")).merge("stat")

    def check():
        want = _one(db, "SELECT product(qty) FROM order_line WHERE product_id = 'a'")[0]
        assert _one(db, "SELECT p FROM stat WHERE product_id = 'a'")[0] == pytest.approx(want)

    r1 = [(1, "a", 2), (2, "a", 3), (3, "a", -4)]
    ol(r1, ts(1))
    run(ts(1), NEVER)
    check()
    r2 = r1 + [(4, "a", 5)]
    ol(r2, ts(2))
    run(ts(2), ts(1))
    check()
    ol([(3, "a", 4) if o == 3 else (o, g, q) for o, g, q in r2], ts(3))  # the sign flips
    run(ts(3), ts(2))
    check()


def test_builder_agg_reduce_ordered(db):
    """``agg.reduce``: a custom order-dependent reduction (here last minus first), re-folded when an edit or a
    deletion changes a group's history."""

    def emit(rows_, f):
        merge(db, "ev", rows_, "id, g, t, x", f, "id")

    def net(state, row):
        first = state[0] if state[0] is not None else row["x"]
        return [first], row["x"] - first

    def run(f, pf):
        at(db, f, pf).source("ev").along("t").aggregate(by="g", chg=agg.reduce(net, [None])).merge("red")

    def reduced():
        return dict(rows(db, "red", "g, chg"))

    emit([(1, "a", 1, 10), (2, "a", 2, 20), (3, "a", 3, 30), (6, "b", 1, 5), (7, "b", 2, 8)], ts(1))
    run(ts(1), NEVER)
    assert reduced() == {"a": 20, "b": 3}
    emit([(1, "a", 1, 10), (2, "a", 2, 20), (3, "a", 3, 30), (4, "a", 4, 50), (6, "b", 1, 5), (7, "b", 2, 8)], ts(2))
    run(ts(2), ts(1))  # a tail append resumes from the carried state
    assert reduced() == {"a": 40, "b": 3}
    emit([(1, "a", 1, 4), (2, "a", 2, 20), (3, "a", 3, 30), (4, "a", 4, 50), (6, "b", 1, 5), (7, "b", 2, 8)], ts(3))
    run(ts(3), ts(2))  # an edit to the first row re-folds the group
    assert reduced() == {"a": 46, "b": 3}
    emit([(1, "a", 1, 4), (2, "a", 2, 20), (3, "a", 3, 30), (6, "b", 1, 5), (7, "b", 2, 8)], ts(4))
    run(ts(4), ts(3))  # deleting the last row re-folds too
    assert reduced() == {"a": 26, "b": 3}


def test_builder_aggregate_guards(db):
    t = at(db, ts(1))
    with pytest.raises(BuildError, match="metric"):
        t.source("a").aggregate(by="k", bad="not a metric")
    with pytest.raises(BuildError, match="group key|by"):
        t.source("a").aggregate(total=agg.sum("x"))
    with pytest.raises(BuildError, match="aggregate"):
        t.source("a").aggregate(by="k", n=agg.count()).append("out")
    with pytest.raises(BuildError, match="aggregate"):
        t.source("a").aggregate(by="k", n=agg.count()).select("k")


# ─── order-dependent scans ───────────────────────────────────────────────────────


def _emit(db, rows_, f):
    """Append only the new (tail) events each run."""
    T.append_table(db, "ev", db.sql(
        f"SELECT * FROM (VALUES {', '.join(f'({i}, {g!r}, {t}, {x})' for i, g, t, x in rows_)}) v(id, g, t, x)"),
        f, ("id",))


def test_builder_accumulate_scan(db):
    """Running values continue from the carried fold state across an incremental run that appends tail rows.
    Linear metrics are checked by hand; ema and tema against an independent fold."""

    def run(f, pf):
        (at(db, f, pf).source("ev").along("t")
         .accumulate(by="g", cs=acc.sum("x"), rc=acc.count(), rmax=acc.max("x"), f0=acc.first("x"),
                     e=acc.ema("x", 0.5), td=acc.tema("x", lam=0.1))
         .append("scored", pk="id"))

    def scored():
        return {r[0]: r for r in rows(db, "scored", "id, g, t, x, cs, rc, rmax, f0, e, td")}

    def ema_ref(all_rows):
        out, st = {}, {}
        for i, g, t, x in sorted(all_rows, key=lambda r: (r[1], r[2])):
            e_prev, td_prev, t_prev = st.get(g, (None, None, None))
            e = x if e_prev is None else 0.5 * x + 0.5 * e_prev
            if td_prev is None:
                td = x
            else:
                a = 1 - math.exp(-0.1 * (t - t_prev))
                td = a * x + (1 - a) * td_prev
            st[g] = (e, td, t)
            out[i] = (e, td)
        return out

    first = [(1, "a", 1, 10), (2, "a", 2, 20), (3, "a", 3, 30), (4, "b", 1, 5), (5, "b", 2, 15)]
    _emit(db, first, ts(1))
    run(ts(1), NEVER)
    got = scored()
    assert got[1][4:8] == (10, 1, 10, 10) and got[3][4:8] == (60, 3, 30, 10)
    assert got[4][4:8] == (5, 1, 5, 5) and got[5][4:8] == (20, 2, 15, 5)
    for i, (e, td) in ema_ref(first).items():
        assert got[i][8] == pytest.approx(e) and got[i][9] == pytest.approx(td)

    new = [(6, "a", 4, 40), (7, "b", 3, 25)]
    _emit(db, new, ts(2))
    run(ts(2), ts(1))
    got = scored()
    assert got[6][4:8] == (100, 4, 40, 10) and got[7][4:8] == (45, 3, 25, 5)  # continued, not restarted
    ref = ema_ref(first + new)
    for i in (6, 7):
        assert got[i][8] == pytest.approx(ref[i][0]) and got[i][9] == pytest.approx(ref[i][1])
    assert got[1][4:8] == (10, 1, 10, 10)  # earlier rows unchanged


def test_builder_accumulate_product_and_scan(db):
    """``acc.product`` and ``acc.scan`` (a custom fold with JSON state) across a tail run."""

    def range_fold(state, row):  # the running range: max so far minus min so far
        lo = row["x"] if state[0] is None else min(state[0], row["x"])
        hi = row["x"] if state[1] is None else max(state[1], row["x"])
        return [lo, hi], hi - lo

    def run(f, pf):
        (at(db, f, pf).source("ev").along("t")
         .accumulate(by="g", prod=acc.product("x"), rng=acc.scan(range_fold, [None, None], dtype="BIGINT"))
         .append("scored", pk="id"))

    _emit(db, [(1, "a", 1, 2), (2, "a", 2, 5), (3, "a", 3, 3)], ts(1))
    run(ts(1), NEVER)
    got = {r[0]: r[1:] for r in rows(db, "scored", "id, prod, rng")}
    assert got[1] == (2, 0) and got[2] == (10, 3) and got[3] == (30, 3)
    _emit(db, [(4, "a", 4, 4)], ts(2))
    run(ts(2), ts(1))
    assert dict((r[0], r[1:]) for r in rows(db, "scored", "id, prod, rng"))[4] == (120, 3)


def test_builder_accumulate_fast_path(db):
    """The SQL-window fast path matches a from-scratch window over the full set, at bootstrap and when it
    continues from the carried seed."""

    def run(f, pf):
        (at(db, f, pf).source("ev").along("t")
         .accumulate(by="g", cs=acc.sum("x"), rc=acc.count(), mn=acc.min("x"), mx=acc.max("x"), f0=acc.first("x"))
         .append("scored", pk="id"))

    def scored():
        return {r[0]: r[1:] for r in rows(db, "scored", "id, cs, rc, mn, mx, f0")}

    def expected():
        return {r[0]: r[1:] for r in db.sql(
            "SELECT id, sum(x) OVER w, CAST(count(*) OVER w AS BIGINT), min(x) OVER w, max(x) OVER w, "
            "first_value(x) OVER w FROM ev "
            "WINDOW w AS (PARTITION BY g ORDER BY t ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)").fetchall()}

    _emit(db, [(1, "a", 1, 10), (2, "a", 2, 5), (3, "a", 3, 20), (4, "b", 1, 3), (5, "b", 2, 8)], ts(1))
    run(ts(1), NEVER)
    assert scored() == expected()
    _emit(db, [(6, "a", 4, 2), (7, "b", 3, 1)], ts(2))
    run(ts(2), ts(1))
    assert scored() == expected()
    assert scored()[6] == (37, 4, 2, 20, 10)


def test_builder_accumulate_lag_and_convolution(db):
    """``acc.prev``, ``acc.lag(n)`` and ``acc.convolution`` reach back across the run boundary."""

    def run(f, pf):
        (at(db, f, pf).source("ev").along("t")
         .accumulate(by="g", p1=acc.prev("x"), p2=acc.lag("x", 2), smooth=acc.convolution("x", [1, 1, 1]))
         .append("scored", pk="id"))

    def scored():
        return {r[0]: r[1:] for r in rows(db, "scored", "id, p1, p2, smooth")}

    _emit(db, [(1, "a", 1, 10), (2, "a", 2, 20), (3, "a", 3, 30)], ts(1))
    run(ts(1), NEVER)
    got = scored()
    assert got[1] == (None, None, None) and got[2] == (10, None, None) and got[3] == (20, 10, 60)
    _emit(db, [(4, "a", 4, 40), (5, "a", 5, 50)], ts(2))
    run(ts(2), ts(1))
    got = scored()
    assert got[4] == (30, 20, 90) and got[5] == (40, 30, 120)


def test_builder_accumulate_merge_retraction(db):
    """Merge-mode scans: an edit to a past row re-folds the group from that row on, and a deletion drops the
    row and re-folds; a tail append resumes from the carried state."""

    def emit(rows_, f):
        merge(db, "ev", rows_, "id, g, t, x", f, "id")

    def run(f, pf):
        at(db, f, pf).source("ev").along("t").accumulate(by="g", cs=acc.sum("x")).merge("scored", pk="id")

    def scored():
        return dict(rows(db, "scored", "id, cs"))

    emit([(1, "a", 1, 10), (2, "a", 2, 20), (3, "a", 3, 30)], ts(1))
    run(ts(1), NEVER)
    assert scored() == {1: 10, 2: 30, 3: 60}
    emit([(1, "a", 1, 10), (2, "a", 2, 20), (3, "a", 3, 30), (4, "a", 4, 40)], ts(2))
    run(ts(2), ts(1))
    assert scored() == {1: 10, 2: 30, 3: 60, 4: 100}
    emit([(1, "a", 1, 10), (2, "a", 2, 25), (3, "a", 3, 30), (4, "a", 4, 40)], ts(3))
    run(ts(3), ts(2))
    assert scored() == {1: 10, 2: 35, 3: 65, 4: 105}
    emit([(1, "a", 1, 10), (2, "a", 2, 25), (4, "a", 4, 40)], ts(4))
    run(ts(4), ts(3))
    assert scored() == {1: 10, 2: 35, 4: 75}


def test_builder_accumulate_guards(db):
    t = at(db, ts(1))
    with pytest.raises(BuildError, match="order axis|along"):
        t.source("a").accumulate(by="g", cs=acc.sum("x"))
    with pytest.raises(BuildError, match="pk"):  # merge mode needs an output pk
        t.source("a").along("t").accumulate(by="g", cs=acc.sum("x")).merge("o")
    with pytest.raises(BuildError, match="acc"):
        t.source("a").along("t").accumulate(by="g", bad="nope")
