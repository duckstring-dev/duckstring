"""The Trickle builder: incremental joins (DBSP composition over the changed keys), filter and mutate,
the change-fraction threshold and strategy escapes, build-time errors, chained intermediates, the
``.append()`` terminal and its spine-key fast path, ``.sql()``/``.schema()``, ``.count()``, and
``was_changed()``. Sources and outputs share one database; each run is a ``trickle.at`` window."""

from __future__ import annotations

from decimal import Decimal

import pytest
from trickle import agg
from trickle.builder import BuildError, TrickleBuilder, _duckdb_to_ibis
from trickle_support import NEVER, T, at, merge, rows, spy_paths, star, ts


def _priced(db, f, previous_f, *, spine_p=0.3, dim_p=0.3):
    t = at(db, f, previous_f)
    (t.source("order_line", p=spine_p)
      .join(t.source("product", p=dim_p), on="product_id")
      .select("s0.order_id, s0.product_id, s0.qty, s1.price, s0.qty * s1.price AS total")
      .merge("priced", pk="order_id"))


def _facts(db, state, f):
    merge(db, "fact", state, "id, k, qty", f, "id")


def _prices(db, state, f):
    merge(db, "dim", state, "k, price", f, "k")


# ─── joins over changed keys ─────────────────────────────────────────────────────


def test_builder_dimension_change_incremental(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _priced(db, ts(1), NEVER)  # bootstrap
    assert rows(db, "priced", "order_id, total") == [(10, 10), (11, 9)]

    pr([("p1", 50), ("p2", 9)], ts(2))  # p1 price 5 → 50; only order 10 uses p1
    _priced(db, ts(2), ts(1))
    assert rows(db, "priced", "order_id, total") == [(10, 100), (11, 9)]


def test_builder_key_prefilter_restricts_spine_on_dim_change(db):
    """The affected-key recompute pre-filters both join inputs to the changed join keys (``IN (SELECT k0 …)``)
    before the join, so a small dimension change never drives a full spine scan. Guards the pushdown by
    inspecting the generated SQL."""
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _priced(db, ts(1), NEVER)

    pr([("p1", 50), ("p2", 9)], ts(2))  # only the dimension changes this run
    t = at(db, ts(2), ts(1))
    b = (t.source("order_line").join(t.source("product", p=1.0), on="product_id").select("s0.order_id, s1.price"))
    seen: list[str] = []
    orig_view = b._view
    b._view = lambda sql: seen.append(sql) or orig_view(sql)  # capture the raw generated SQL
    kind, _rel = b._compute(("order_id",), "priced")
    assert kind == "incremental"
    assert any('"s0.product_id") IN (SELECT k0' in s for s in seen)  # the spine filtered to the changed keys


def test_builder_propagates_spine_delete(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _priced(db, ts(1), NEVER)
    ol([(10, "p1", 2)], ts(2))  # order 11 removed from the spine
    _priced(db, ts(2), ts(1))
    assert rows(db, "priced", "order_id, total") == [(10, 10)]


def test_builder_fact_and_dim_both_change(db):
    """Fact and dim both change in one run. The telescoping sum needs the dim's old state: the intermediate
    row priced at the old price must cancel."""
    _facts(db, [(1, "A", 10), (2, "A", 5), (3, "B", 7)], ts(1))
    _prices(db, [("A", 100), ("B", 200)], ts(1))

    def run(f, pf):
        t = at(db, f, pf)
        t.source("fact").join(t.source("dim"), on="k").select("s0.id, s0.k, s0.qty, s1.price").merge("o", pk="id")

    run(ts(1), NEVER)
    # Run 2: fact updates id 2 (qty 5→8) and inserts id 4; dim updates A (price 100→120).
    _facts(db, [(1, "A", 10), (2, "A", 8), (3, "B", 7), (4, "B", 2)], ts(2))
    _prices(db, [("A", 120), ("B", 200)], ts(2))
    run(ts(2), ts(1))
    assert rows(db, "o", "id, k, qty, price") == [(1, "A", 10, 120), (2, "A", 8, 120), (3, "B", 7, 200),
                                                   (4, "B", 2, 200)]


def test_builder_rebuilds_whole_when_output_dropped(db):
    """After the output is dropped, the next run rebuilds the whole table through the comprehensive path,
    not just the run's small delta (which, appended to an empty log, would leave only the changed rows)."""
    _facts(db, [(1, "A", 10), (2, "A", 5), (3, "B", 7)], ts(1))
    _prices(db, [("A", 100), ("B", 200)], ts(1))

    def run(f, pf):
        t = at(db, f, pf)
        t.source("fact").join(t.source("dim"), on="k").select("s0.id, s0.k, s0.qty, s1.price").merge("o", pk="id")

    run(ts(1), NEVER)
    assert rows(db, "o", "id, k, qty, price") == [(1, "A", 10, 100), (2, "A", 5, 100), (3, "B", 7, 200)]
    T.drop_table(db, "o")
    assert "o" not in T.read_meta(db)

    _facts(db, [(1, "A", 10), (2, "A", 8), (3, "B", 7)], ts(2))  # a small change, with a recent previous_f
    run(ts(2), ts(1))
    assert rows(db, "o", "id, k, qty, price") == [(1, "A", 10, 100), (2, "A", 8, 100), (3, "B", 7, 200)]


def test_builder_matches_comprehensive_row_for_row(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _priced(db, ts(1), NEVER)
    pr([("p1", 50), ("p2", 9)], ts(2))
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3), (13, "p2", 4)], ts(2))  # add order 13
    _priced(db, ts(2), ts(1))
    truth = sorted(db.sql("SELECT ol.order_id, ol.qty * pr.price FROM order_line ol "
                          "JOIN product pr USING (product_id)").fetchall())
    assert rows(db, "priced", "order_id, total") == truth


def test_builder_joins_on_non_pk_business_code(db):
    """Join on any column (a business code), not the dimension's PK, and a deletion still propagates (it is a
    full-row retraction, not a key-only tombstone)."""
    merge(db, "fact", [(1, "X"), (2, "Y"), (3, "X")], "id, code", ts(1), "id")
    merge(db, "dim", [("s1", "X", "xan"), ("s2", "Y", "yur")], "sku, code, label", ts(1), "sku")

    def run(f, pf):
        t = at(db, f, pf)
        t.source("fact").join(t.source("dim"), on="code").select("s0.id, s0.code, s1.label").merge("o", pk="id")

    run(ts(1), NEVER)
    assert rows(db, "o", "id, label") == [(1, "xan"), (2, "yur"), (3, "xan")]
    merge(db, "dim", [("s1", "X", "xan")], "sku, code, label", ts(2), "sku")  # code Y deleted, by its sku
    run(ts(2), ts(1))
    assert rows(db, "o", "id, label") == [(1, "xan"), (3, "xan")]


def test_builder_absorbs_coverage_miss_comprehensively(db):
    """A consumer whose window starts before the source's retained history recomputes in full, so a delete
    it never saw as a change still drops out."""
    pr = star(db)[1]
    pr([("p1", 5), ("p2", 9)], ts(1))

    def order_lines(state, f):
        merge(db, "order_line", state, "order_id, product_id", f, "order_id", retain_n=1)

    def build(f, previous_f):
        t = at(db, f, previous_f)
        (t.source("order_line").join(t.source("product"), on="product_id")
          .select("s0.order_id, s0.product_id, s1.price").merge("priced", pk="order_id"))

    order_lines([(10, "p1"), (11, "p2")], ts(1))
    build(ts(1), NEVER)
    assert rows(db, "priced", "order_id") == [(10,), (11,)]
    order_lines([(10, "p1")], ts(2))  # 11 removed
    order_lines([(10, "p1")], ts(3))  # and retention moves the floor past ts(1)
    build(ts(3), ts(1))
    assert rows(db, "priced", "order_id") == [(10,)]


# ─── filter and mutate ───────────────────────────────────────────────────────────


def test_builder_filter_applies_to_delta_and_crosses_boundary(db):
    """``.filter()`` distributes over the change: a dimension change that pushes a row across the filter
    boundary inserts or retracts it incrementally."""
    ol, pr = star(db)

    def run(f, pf):
        t = at(db, f, pf)
        (t.source("order_line", p=1.0).alias("o")
          .join(t.source("product", p=1.0).alias("p"), on="product_id")
          .filter("o.qty * p.price >= 10")
          .select("o.order_id, o.qty * p.price AS total")
          .merge("out", pk="order_id"))

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)  # totals 10 and 9: only order 10 passes
    assert rows(db, "out", "order_id, total") == [(10, 10)]
    pr([("p1", 5), ("p2", 20)], ts(2))  # order 11's total becomes 20 and crosses in
    run(ts(2), ts(1))
    assert rows(db, "out", "order_id, total") == [(10, 10), (11, 20)]
    pr([("p1", 3), ("p2", 20)], ts(3))  # order 10's total becomes 6 and drops out
    run(ts(3), ts(2))
    assert rows(db, "out", "order_id, total") == [(11, 20)]


def test_builder_mutate_computed_column_and_pk(db):
    """``.mutate()`` adds computed columns and keeps the others (the ``*`` default), takes parallel columns in
    one call and a chained mutate building on an earlier one, and a mutated column can be the merge pk."""
    ol, pr = star(db)

    def run(f, pf):
        t = at(db, f, pf)
        (t.source("order_line").alias("o")
          .join(t.source("product").alias("p"), on="product_id")
          .mutate(line_id="o.order_id || '-' || o.product_id", total="o.qty * p.price")
          .mutate(total_with_tax="total * 1.1")
          .merge("m", pk="line_id"))

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "m", "line_id, total, total_with_tax") == [
        ("10-p1", 10, Decimal("11.0")), ("11-p2", 9, Decimal("9.9"))]
    assert rows(db, "m", "order_id, product_id, qty, price") == [(10, "p1", 2, 5), (11, "p2", 1, 9)]
    pr([("p1", 50), ("p2", 9)], ts(2))  # only 10-p1 recomputes
    run(ts(2), ts(1))
    assert rows(db, "m", "line_id, total, total_with_tax") == [
        ("10-p1", 100, Decimal("110.0")), ("11-p2", 9, Decimal("9.9"))]


def test_builder_filter_on_mutated_column(db):
    """A ``.filter()`` after a ``.mutate()`` can reference the mutated column, and still distributes over the
    change."""
    ol, pr = star(db)

    def run(f, pf):
        t = at(db, f, pf)
        (t.source("order_line", p=1.0).alias("o")
          .join(t.source("product", p=1.0).alias("p"), on="product_id")
          .mutate(total="o.qty * p.price")
          .filter("total >= 10")
          .select("o.order_id, total")
          .merge("fm", pk="order_id"))

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "fm", "order_id, total") == [(10, 10)]
    pr([("p1", 5), ("p2", 20)], ts(2))
    run(ts(2), ts(1))
    assert rows(db, "fm", "order_id, total") == [(10, 10), (11, 20)]
    pr([("p1", 3), ("p2", 20)], ts(3))
    run(ts(3), ts(2))
    assert rows(db, "fm", "order_id, total") == [(11, 20)]


def test_builder_mutate_errors(db):
    t = at(db, ts(1))
    with pytest.raises(BuildError, match="at least one"):
        t.source("order_line").mutate()
    with pytest.raises(BuildError, match="reserved"):
        t.source("order_line").mutate(_duckstring_x="1")
    with pytest.raises(BuildError, match="operand"):  # a join operand can't carry a .mutate()
        t.source("order_line").join(t.source("product").mutate(x="price * 2"), on="product_id")


# ─── the change-fraction threshold and the strategy escapes ──────────────────────


def test_builder_threshold_falls_back_to_comprehensive(db, monkeypatch):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p3", 3)], ts(1))
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(1))
    _priced(db, ts(1), NEVER)
    paths = spy_paths(monkeypatch)
    pr([("p1", 50), ("p2", 90), ("p3", 70)], ts(2))  # 3 of 3 prices change: 100% > p=0.3
    _priced(db, ts(2), ts(1))
    assert paths == ["comprehensive"]
    assert rows(db, "priced", "order_id, total") == [(10, 100), (11, 90), (12, 210)]


def test_builder_threshold_p1_forces_incremental(db, monkeypatch):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p3", 3)], ts(1))
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(1))
    _priced(db, ts(1), NEVER, dim_p=1.0)
    paths = spy_paths(monkeypatch)
    pr([("p1", 50), ("p2", 90), ("p3", 70)], ts(2))  # 100% churn, but dim_p=1.0 keeps it incremental
    _priced(db, ts(2), ts(1), dim_p=1.0)
    assert paths == ["incremental"]
    assert rows(db, "priced", "order_id, total") == [(10, 100), (11, 90), (12, 210)]


def test_builder_ivm_false_forces_comprehensive(db, monkeypatch):
    """``ivm=False`` ignores deltas: even a change well under ``p`` recomputes the whole output and diffs it."""
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p3", 3)], ts(1))
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(1))

    def build(f, pf, *, ivm=True):
        t = at(db, f, pf)
        (t.source("order_line").join(t.source("product"), on="product_id")
          .select("s0.order_id, s0.qty * s1.price AS total").merge("priced", pk="order_id", ivm=ivm))

    build(ts(1), NEVER)
    paths = spy_paths(monkeypatch)
    pr([("p1", 50), ("p2", 9), ("p3", 7)], ts(2))
    build(ts(2), ts(1), ivm=False)
    assert paths == ["comprehensive"]
    assert rows(db, "priced", "order_id, total") == [(10, 100), (11, 9), (12, 21)]


def test_builder_key_filter_false_skips_in_filter(db):
    """``key_filter=False`` keeps the delta composition but drops the ``IN (SELECT k0 …)`` pre-filter."""
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))

    def build(f, pf, *, key_filter=True, capture=None):
        t = at(db, f, pf)
        b = (t.source("order_line").join(t.source("product", p=1.0), on="product_id")
              .select("s0.order_id, s0.qty * s1.price AS total"))
        if capture is not None:
            orig = b._view
            b._view = lambda sql: capture.append(sql) or orig(sql)
        b.merge("priced", pk="order_id", key_filter=key_filter)

    build(ts(1), NEVER)
    pr([("p1", 50), ("p2", 9)], ts(2))
    seen: list[str] = []
    build(ts(2), ts(1), key_filter=False, capture=seen)
    assert not any("IN (SELECT k0" in s for s in seen)
    assert rows(db, "priced", "order_id, total") == [(10, 100), (11, 9)]


# ─── build-time errors ───────────────────────────────────────────────────────────


def test_builder_build_time_errors(db):
    ol, pr = star(db)
    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    t = at(db, ts(1))

    # A composed operand (a join DAG) is a valid join input.
    composed = t.source("product").alias("p1").join(t.source("product").alias("p2"), on="product_id")
    t.source("order_line").join(composed, on="product_id")

    # A join operand carrying its own .select() is rejected (attach it to the result).
    with pytest.raises(BuildError, match="operand"):
        t.source("order_line").join(t.source("product").select("product_id"), on="product_id")

    # A joined graph with no .select(...) resolves through the bare `*` (the join key deduplicated).
    t.source("order_line").join(t.source("product"), on="product_id").merge("x", pk="order_id")
    assert rows(db, "x", "order_id, price") == [(10, 5)]

    # A `*` with a genuine (non-join-key) name collision raises: p1 and p2 both carry `price`.
    with pytest.raises(BuildError, match="ambiguous"):
        (t.source("order_line")
          .join(t.source("product").alias("p1").join(t.source("product").alias("p2"), on="product_id"),
                on="product_id")
          .merge("y", pk="order_id"))
    with pytest.raises(BuildError, match="output key"):  # pk only defaults after .aggregate()
        t.source("order_line").join(t.source("product"), on="product_id").select("s0.order_id, s1.price").merge("x")
    with pytest.raises(BuildError, match="key"):
        (t.source("order_line").join(t.source("product"), on="product_id")
          .select("s0.order_id, s1.price").merge("x", pk=()))
    with pytest.raises(BuildError, match="PK"):  # the projection omits the pk
        t.source("order_line").join(t.source("product"), on="product_id").select("s1.price").merge("x", pk="order_id")


# ─── chained merges: an intermediate materialised mid-chain ──────────────────────


def _chain(db):
    """Sources for a genuine chain a→b→c: c joins on a column only a⋈b produces, so a single star can't
    express it and the intermediate must be materialised."""

    def a(state, f):  # order lines: order_id → product_id
        merge(db, "ol", state, "order_id, product_id, qty", f, "order_id")

    def b(state, f):  # product → category
        merge(db, "prod", state, "product_id, category", f, "product_id")

    def c(state, f):  # category → tax
        merge(db, "tax", state, "category, tax", f, "category")

    def run(f, pf):
        t = at(db, f, pf)
        ab = (t.source("ol").join(t.source("prod"), on="product_id")
               .select("s0.order_id, s0.product_id, s0.qty, s1.category").merge("ab", pk="order_id"))
        ab.join(t.source("tax"), on="category").select("s0.order_id, s0.qty, s0.category, s1.tax") \
          .merge("abc", pk="order_id")

    return a, b, c, run


def test_builder_chains_through_materialised_intermediate(db):
    a, b, c, run = _chain(db)
    a([(10, "p1", 2), (11, "p2", 1)], ts(1))
    b([("p1", "books"), ("p2", "food")], ts(1))
    c([("books", 10), ("food", 5)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "abc", "order_id, category, tax") == [(10, "books", 10), (11, "food", 5)]
    assert rows(db, "ab", "order_id, category") == [(10, "books"), (11, "food")]  # the intermediate is stored

    c([("books", 20), ("food", 5)], ts(2))  # a c-only change: ab untouched, only order 10 flows to abc
    run(ts(2), ts(1))
    assert rows(db, "ab", "order_id, category") == [(10, "books"), (11, "food")]
    assert rows(db, "abc", "order_id, category, tax") == [(10, "books", 20), (11, "food", 5)]

    a([(10, "p1", 2), (11, "p1", 1)], ts(3))  # an a-only change flows through the stored ab
    run(ts(3), ts(2))
    assert rows(db, "ab", "order_id, category") == [(10, "books"), (11, "books")]
    assert rows(db, "abc", "order_id, category, tax") == [(10, "books", 20), (11, "books", 20)]


def test_builder_chain_matches_comprehensive_join(db):
    a, b, c, run = _chain(db)
    a([(10, "p1", 2), (11, "p2", 1), (12, "p3", 4)], ts(1))
    b([("p1", "books"), ("p2", "food"), ("p3", "books")], ts(1))
    c([("books", 10), ("food", 5)], ts(1))
    run(ts(1), NEVER)
    a([(10, "p1", 2), (11, "p2", 1), (12, "p3", 9), (13, "p1", 1)], ts(2))  # 12 changes, 13 is new
    b([("p1", "media"), ("p2", "food"), ("p3", "books")], ts(2))            # p1 books → media
    c([("books", 10), ("food", 7), ("media", 3)], ts(2))                    # food's tax changes, media is new
    run(ts(2), ts(1))
    want = sorted(db.sql("SELECT ol.order_id, prod.category, tax.tax FROM ol JOIN prod USING (product_id) "
                         "JOIN tax USING (category)").fetchall())
    assert rows(db, "abc", "order_id, category, tax") == want


# ─── the .append() terminal ──────────────────────────────────────────────────────


def _enriched(db, f, pf, *, fail_on_conflict=True, log_drops=True, dim_p=0.3):
    """Append-enrich the order-line spine with the product dim (one output row per order_id). ``dim_p=1.0``
    keeps a change to the tiny dim on the incremental path."""
    t = at(db, f, pf)
    (t.source("order_line").join(t.source("product", p=dim_p), on="product_id")
      .select("s0.order_id, s0.product_id, s0.qty, s1.price")
      .append("enriched", pk="order_id", fail_on_conflict=fail_on_conflict, log_drops=log_drops))


def test_builder_append_monotonic_accumulates_history(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _enriched(db, ts(1), NEVER)
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9)]
    assert T.read_meta(db)["enriched"]["mode"] == "append"
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(2))  # a new order
    _enriched(db, ts(2), ts(1))
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 5)]


def test_builder_append_fails_on_changed_past(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _enriched(db, ts(1), NEVER)
    pr([("p1", 50), ("p2", 9)], ts(2))  # p1's reprice retracts order 10's row: not append-safe
    with pytest.raises(T.DeltaError, match="not append-safe|retraction|changed-past"):
        _enriched(db, ts(2), ts(1))


def test_builder_append_drops_conflicts_and_logs(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _enriched(db, ts(1), NEVER)
    pr([("p1", 50), ("p2", 9)], ts(2))
    _enriched(db, ts(2), ts(1), fail_on_conflict=False, dim_p=1.0)
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9)]  # order 10 keeps its original price
    # Both dropped rows are logged: the retraction (-1, old price) and the changed-image insert (+1, new).
    drops = db.sql('SELECT order_id, price, _duckstring_d FROM "enriched__droplog" ORDER BY _duckstring_d').fetchall()
    assert drops == [(10, 5, -1), (10, 50, 1)]
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p2", 4)], ts(3))  # a new order still flows through
    _enriched(db, ts(3), ts(2), fail_on_conflict=False)
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 9)]


def test_builder_append_log_drops_false_squashes_table(db):
    ol, pr = star(db)
    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    _enriched(db, ts(1), NEVER, fail_on_conflict=False, log_drops=False)
    pr([("p1", 50)], ts(2))
    _enriched(db, ts(2), ts(1), fail_on_conflict=False, log_drops=False)
    assert not T._table_exists(db, "enriched__droplog")


def test_spine_pk_passthrough_detection():
    def detect(projection, out_pk, spine_pk, spine_alias=None):
        b = TrickleBuilder(None, "spine")
        b._ops = [("select", projection)]
        b._alias = spine_alias
        return b._spine_pk_passthrough(out_pk, spine_pk)

    # Verbatim s0 pass-throughs (single, renamed, composite) are detected...
    assert detect("s0.order_id, s1.price", ("order_id",), ("order_id",)) == {"order_id": "order_id"}
    # ...and the same off a custom spine alias.
    assert detect("o.order_id, p.price", ("order_id",), ("order_id",), spine_alias="o") == {"order_id": "order_id"}
    assert detect("s0.order_id, p.price", ("order_id",), ("order_id",), spine_alias="o") is None
    assert detect("s0.oid AS order_id, s1.price", ("order_id",), ("oid",)) == {"order_id": "oid"}
    assert detect('s0."a", s0.b, s1.x', ("a", "b"), ("a", "b")) == {"a": "a", "b": "b"}
    assert detect("s0.id, round(s1.a, 2) AS r", ("id",), ("id",)) == {"id": "id"}  # a comma inside a call

    # Conservative bails, falling back to the always-correct general path:
    assert detect("round(s0.x, 2) AS k, s1.y", ("k",), ("x",)) is None                      # computed PK
    assert detect("s1.k, s0.v", ("k",), ("k",)) is None                                      # PK from a dim
    assert detect("s0.order_id, s0.region", ("order_id", "region"), ("order_id",)) is None  # out PK ⊋ spine PK
    assert detect("s0.order_id", ("order_id",), ()) is None                                  # no spine PK


def test_builder_append_spine_pk_fast_path(db, monkeypatch):
    ol, pr = star(db)

    def run(f, pf):  # fail_on_conflict=False, log_drops=False and an s0.order_id PK: the fast path applies
        _enriched(db, f, pf, fail_on_conflict=False, log_drops=False)

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9)]
    pr([("p1", 50), ("p2", 9)], ts(2))  # a dimension-only change touches no spine row: nothing flows
    run(ts(2), ts(1))
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9)]
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(3))  # a new spine row gets the current dim value
    pr([("p1", 70), ("p2", 9)], ts(3))
    run(ts(3), ts(2))
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 70)]

    def _boom(*a, **k):
        raise AssertionError("_compute should not run on the spine-PK fast path")

    monkeypatch.setattr(TrickleBuilder, "_compute", _boom)  # the fast path never composes the change
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3), (13, "p2", 1)], ts(4))
    run(ts(4), ts(3))
    assert rows(db, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 70), (13, 9)]


def test_builder_append_spine_pk_fast_path_same_f_replay(db):
    """A same-epoch replay of a spine-PK append run is idempotent. ``_new_spine_rows`` once counted the first
    attempt's own rows as history, so the replay's ``DELETE(@f)`` dropped them and nothing re-inserted them."""
    ol, pr = star(db)

    def run(f, pf):
        _enriched(db, f, pf, fail_on_conflict=False, log_drops=False)

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(2))
    run(ts(2), ts(1))
    single = rows(db, "enriched", "order_id, price")
    assert single == [(10, 5), (11, 9), (12, 5)]
    run(ts(2), ts(1))  # replay the same epoch with the same input
    assert rows(db, "enriched", "order_id, price") == single


# ─── .alias(), .sql(), .schema() ─────────────────────────────────────────────────


def test_builder_alias_names_sources(db):
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    t = at(db, ts(1))
    (t.source("order_line").alias("ol")
      .join(t.source("product").alias("pr"), on="product_id")
      .select("ol.order_id, ol.qty, pr.price, ol.qty * pr.price AS total")
      .merge("priced", pk="order_id"))
    assert rows(db, "priced", "order_id, total") == [(10, 10), (11, 9)]


def test_builder_sql_aggregation_keeps_incremental_output(db):
    """An incremental join stored with ``.merge()``, aggregated with ``.sql()``, and merged: the aggregate
    rescans, but the output still changes only for the groups that moved."""
    ol, pr = star(db)

    def run(f, pf):
        t = at(db, f, pf)
        priced = (t.source("order_line").alias("o")
                   .join(t.source("product").alias("p"), on="product_id")
                   .select("o.order_id, o.product_id, o.qty * p.price AS revenue")
                   .merge("priced_line", pk="order_id"))
        totals = priced.alias("pl").sql(
            "SELECT product_id, sum(revenue) AS total_revenue, count(*) AS n FROM pl GROUP BY product_id")
        with pytest.raises(BuildError, match="after .sql"):
            totals.join(t.source("product"), on="product_id")
        totals.merge("revenue_by_product", pk="product_id")

    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    assert rows(db, "revenue_by_product", "product_id, total_revenue, n") == [("p1", 25, 2), ("p2", 9, 1)]
    pr([("p1", 10), ("p2", 9)], ts(2))  # only p1 reprices
    run(ts(2), ts(1))
    assert rows(db, "revenue_by_product", "product_id, total_revenue, n") == [("p1", 50, 2), ("p2", 9, 1)]
    latest = db.sql('SELECT DISTINCT product_id FROM "revenue_by_product__changelog" WHERE _duckstring_f = '
                    '(SELECT max(_duckstring_f) FROM "revenue_by_product__changelog")').fetchall()
    assert latest == [("p1",)]


def test_builder_sql_requires_alias_and_uses_star(db):
    ol, pr = star(db)
    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    t = at(db, ts(1))
    with pytest.raises(BuildError, match="alias"):
        t.source("order_line").sql("SELECT 1")
    (t.source("order_line").join(t.source("product"), on="product_id").alias("j")
      .sql("SELECT order_id, qty * price AS total FROM j").merge("x", pk="order_id"))
    assert rows(db, "x", "order_id, total") == [(10, 10)]


def test_duckdb_to_ibis_type_map():
    assert _duckdb_to_ibis("BIGINT") == "int64"
    assert _duckdb_to_ibis("VARCHAR") == "string"
    assert _duckdb_to_ibis("DOUBLE") == "float64"
    assert _duckdb_to_ibis("TIMESTAMP WITH TIME ZONE") == "timestamp('UTC')"
    assert _duckdb_to_ibis("DECIMAL(18,2)") == "decimal(18,2)"
    with pytest.raises(BuildError, match="no Ibis mapping"):
        _duckdb_to_ibis("INTEGER[]")


def test_builder_schema_and_to_ibis_schema(db):
    ol, pr = star(db)
    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    t = at(db, ts(1))
    priced = (t.source("order_line").alias("o")
               .join(t.source("product").alias("p"), on="product_id")
               .select("CAST(o.order_id AS BIGINT) AS order_id, CAST(o.qty * p.price AS DOUBLE) AS revenue")
               .merge("priced", pk="order_id"))
    assert priced.schema() == {"order_id": "BIGINT", "revenue": "DOUBLE"}
    assert priced.to_ibis_schema() == {"order_id": "int64", "revenue": "float64"}


def test_builder_sql_accepts_ibis_expression(db):
    ibis = pytest.importorskip("ibis")
    ol, pr = star(db)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    t = at(db, ts(1))
    priced = (t.source("order_line").alias("o")
               .join(t.source("product").alias("p"), on="product_id")
               .select("o.order_id, o.product_id, o.qty * p.price AS revenue")
               .merge("priced_line", pk="order_id"))
    pl = ibis.table(priced.to_ibis_schema(), name="pl")
    priced.alias("pl").sql(pl.group_by("product_id").aggregate(total_revenue=pl.revenue.sum())) \
        .merge("revenue_by_product", pk="product_id")
    assert rows(db, "revenue_by_product", "product_id, total_revenue") == [("p1", 25), ("p2", 9)]


# ─── .count() and was_changed() ──────────────────────────────────────────────────


def test_builder_count(db):
    """``.count()``: a stored Trickle counts from metadata and the changelog's net weight; a composed query is
    evaluated and counted. Both track inserts and deletes."""
    _prices(db, [("A", 100), ("B", 200)], ts(1))
    seen = {}
    proj = "s0.id, s0.k, s0.qty, s1.price"

    def run(f, pf):
        t = at(db, f, pf)
        t.source("fact").join(t.source("dim"), on="k").select(proj).merge("o", pk="id")
        seen["merge"] = t.source("o").count()       # a stored merge main: the metadata path
        seen["source"] = t.source("fact").count()   # a source: the host's count_table
        seen["query"] = t.source("fact").join(t.source("dim"), on="k").select(proj).count()  # evaluated

    _facts(db, [(1, "A", 10), (2, "A", 5), (3, "B", 7)], ts(1))
    run(ts(1), NEVER)
    assert seen == {"merge": 3, "source": 3, "query": 3}
    _facts(db, [(1, "A", 10), (2, "A", 8), (3, "B", 7), (4, "B", 2)], ts(2))
    run(ts(2), ts(1))
    assert seen == {"merge": 4, "source": 4, "query": 4}
    _facts(db, [(1, "A", 10), (3, "B", 7), (4, "B", 2)], ts(3))
    run(ts(3), ts(2))
    assert seen == {"merge": 3, "source": 3, "query": 3}  # a delete: the net weight goes down
    assert seen["merge"] == len(rows(db, "o", "id"))


def test_builder_count_after_aggregate_is_group_count(db):
    """``.aggregate(by).count()`` is the number of groups, without running the metrics."""
    _facts(db, [(1, "A", 10), (2, "A", 5), (3, "B", 7)], ts(1))
    assert at(db, ts(1)).source("fact").aggregate(by="k", qty=agg.sum("qty")).count() == 2


def test_builder_was_changed_tracks_output(db):
    ol, pr = star(db)

    def run(f, pf):
        t = at(db, f, pf)
        out = (t.source("order_line", p=1.0).join(t.source("product", p=1.0), on="product_id")
                .select("s0.order_id, s0.qty * s1.price AS total").merge("out", pk="order_id"))
        return out.was_changed()

    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    assert run(ts(1), NEVER) is True   # the bootstrap writes output
    assert run(ts(2), ts(1)) is False  # nothing upstream changed: an empty change
    pr([("p1", 9)], ts(3))
    assert run(ts(3), ts(2)) is True
