"""One conflict rule for append tables (plans/showcase-fixes.md item 19). ``fail_on_conflict=False`` skipped the key
check on ``append_table`` but dropped existing keys on the builder's ``.append()``, and the builder had no write that
skipped the check at all. Now both take ``on_conflict``: ``skip_check`` (the default; reads nothing), ``keep_prior``
or ``fail``. A builder Run it has to recompute in full adds only what the table doesn't hold yet."""

from __future__ import annotations

import duckdb
import pytest
from trickle_support import NEVER, T, at, merge, ts, values


def _all(con, table, cols):
    return sorted(con.sql(f'SELECT {cols} FROM "{table}"').fetchall())


def _append(con, rows_, f, *, pk="id", cols="id, v", **kw):
    return at(con, f).append_table("event", con.sql(values(rows_, cols)), pk=pk, **kw)


# ─── append_table ───────────────────────────────────────────────────────────────


def test_skip_check_appends_every_row_as_given(db):
    _append(db, [(1, "a")], ts(1))
    _append(db, [(1, "b"), (2, "c"), (2, "c")], ts(2))  # a repeated key, and a row twice in one batch
    assert _all(db, "event", "id, v") == [(1, "a"), (1, "b"), (2, "c"), (2, "c")]


def test_keep_prior_drops_existing_keys_and_logs_only_differing_ones(db):
    kp = {"on_conflict": "keep_prior"}
    _append(db, [(1, "a"), (2, "b")], ts(1), **kp)
    assert _append(db, [(1, "a"), (2, "x"), (3, "c"), (3, "c")], ts(2), **kp)
    assert _all(db, "event", "id, v") == [(1, "a"), (2, "b"), (3, "c")]  # prior kept; identical rows collapse
    assert _all(db, "event__droplog", "id, v, _duckstring_d") == [(2, "x", 1)]  # the identical re-send isn't logged
    assert not _append(db, [(1, "a")], ts(3), **kp)  # nothing new
    with pytest.raises(T.DeltaError, match="different values in one batch"):
        _append(db, [(4, "a"), (4, "b")], ts(4), **kp)


def test_without_a_pk_the_whole_row_is_the_key(db):
    kp = {"on_conflict": "keep_prior", "pk": None}
    _append(db, [(1, "a")], ts(1), **kp)
    _append(db, [(1, "a"), (1, "b")], ts(2), **kp)
    assert _all(db, "event", "id, v") == [(1, "a"), (1, "b")]
    _append(db, [(1, "a")], ts(3), on_conflict="fail", pk=None)  # identical: skipped, not a conflict
    assert _all(db, "event", "id, v") == [(1, "a"), (1, "b")]


def test_a_replay_at_the_same_f_replaces_its_rows_under_every_rule(db):
    for i, rule in enumerate(("skip_check", "keep_prior", "fail")):
        name = f"r{i}"
        ep = at(db, ts(1))
        ep.append_table(name, db.sql(values([(1, "a")], "id, v")), pk="id", on_conflict=rule)
        assert not ep.append_table(name, db.sql(values([(1, "a")], "id, v")), pk="id", on_conflict=rule)
        ep.append_table(name, db.sql(values([(2, "b")], "id, v")), pk="id", on_conflict=rule)  # a retry differing
        assert _all(db, name, "id, v") == [(2, "b")], rule


def test_the_removed_flag_names_its_replacement(db):
    with pytest.raises(TypeError, match="fail_on_conflict=False is now on_conflict='skip_check'"):
        _append(db, [(1, "a")], ts(1), fail_on_conflict=False)
    with pytest.raises(TypeError, match="fail_on_conflict=False is now on_conflict='keep_prior'"):
        at(db, ts(1)).source("event").append("out", pk="id", fail_on_conflict=False)
    with pytest.raises(TypeError, match="fail_on_conflict=True is now on_conflict='fail'"):
        at(db, ts(1)).source("event").append("out", pk="id", fail_on_conflict=True)
    with pytest.raises(ValueError, match="on_conflict must be one of"):
        _append(db, [(1, "a")], ts(1), on_conflict="keep_new")


# ─── the builder's .append() ─────────────────────────────────────────────────────


def _sales(con, rows_, f, retain_n=None):
    """An append-only fact table: (sale_id, product_id, qty)."""
    at(con, f).append_table("sale", con.sql(values(rows_, "sale_id, product_id, qty")), pk="sale_id",
                            retain_n=retain_n)


def _price(con, rows_, f):
    merge(con, "product", rows_, "product_id, price", f, "product_id")


def _enrich(con, f, pf, *, p=1.0, pk="sale_id", **kw):
    t = at(con, f, pf)
    (t.source("sale", p=p).join(t.source("product", p=p), on="product_id")
      .select("s0.sale_id, s0.product_id, s0.qty, s1.price")
      .append("enriched", pk=pk, **kw))


class _Recorder:
    """A connection that records the SQL it runs, to show which tables a write reads."""

    def __init__(self, con):
        self._con, self.sql_log = con, []

    def execute(self, q, *a, **k):
        self.sql_log.append(q)
        return self._con.execute(q, *a, **k)

    def sql(self, q, *a, **k):
        self.sql_log.append(q)
        return self._con.sql(q, *a, **k)

    def __getattr__(self, name):
        return getattr(self._con, name)


def test_skip_check_appends_a_change_without_reading_the_table(db):
    _sales(db, [(1, "p", 2)], ts(1))
    _price(db, [("p", 5)], ts(1))
    _enrich(db, ts(1), NEVER)
    _sales(db, [(2, "p", 3)], ts(2))
    rec = _Recorder(db)
    _enrich(rec, ts(2), ts(1))
    assert _all(db, "enriched", "sale_id, price") == [(1, 5), (2, 5)]
    reads = [q for q in rec.sql_log if '"enriched"' in q
             and not q.lstrip().startswith(("DELETE FROM", "INSERT INTO"))]
    assert rec.sql_log and reads == []  # keep_prior would read it twice


def test_a_dimension_change_leaves_the_same_table_on_either_path(db):
    """Incremental: the change removes and re-adds sale 1, a broken promise, so the key is skipped. Recompute:
    the key is already in the table, so it is skipped too."""
    def run(con, p):
        _sales(con, [(1, "p", 2)], ts(1))
        _price(con, [("p", 5)], ts(1))
        _enrich(con, ts(1), NEVER)
        _price(con, [("p", 50)], ts(2))
        _sales(con, [(2, "p", 1)], ts(2))
        _enrich(con, ts(2), ts(1), p=p)
        return _all(con, "enriched", "sale_id, price")

    incremental = run(duckdb.connect(), 1.0)
    recompute = run(duckdb.connect(), 0.0)  # p=0 recomputes on any change
    assert incremental == recompute == [(1, 5), (2, 50)]


def test_a_recompute_after_a_retention_trim_keeps_what_the_table_holds(db):
    _sales(db, [(1, "p", 2)], ts(1), retain_n=1)
    _price(db, [("p", 5)], ts(1))
    _enrich(db, ts(1), NEVER)
    _sales(db, [(2, "p", 1)], ts(2), retain_n=1)
    _sales(db, [(3, "p", 4)], ts(3), retain_n=1)  # sale 1 and 2 trimmed: the Sink, at ts(1), is behind
    _enrich(db, ts(3), ts(1))
    assert _all(db, "enriched", "sale_id") == [(1,), (3,)]  # sale 1 kept, nothing twice


def test_without_a_pk_repeated_identical_events_survive_a_recompute(db):
    def clicks(rows_, f):
        at(db, f).append_table("click", db.sql(values(rows_, "page, n")))

    def count(f, pf, p):
        t = at(db, f, pf)
        t.source("click", p=p).select("s0.page, s0.n").append("seen")

    clicks([("home", 1), ("home", 1)], ts(1))  # two identical clicks
    count(ts(1), NEVER, 1.0)
    clicks([("home", 1)], ts(2))  # a third
    count(ts(2), ts(1), 0.0)  # recomputed: the history holds two, the output three
    assert _all(db, "seen", "page, n") == [("home", 1)] * 3


def test_fail_and_keep_prior_on_the_builder(db):
    _sales(db, [(1, "p", 2)], ts(1))
    _price(db, [("p", 5)], ts(1))
    _enrich(db, ts(1), NEVER, on_conflict="fail")
    _price(db, [("p", 50)], ts(2))
    with pytest.raises(T.DeltaError, match="not append-safe"):
        _enrich(db, ts(2), ts(1), on_conflict="fail")
    _enrich(db, ts(2), ts(1), on_conflict="keep_prior")
    assert _all(db, "enriched", "sale_id, price") == [(1, 5)]
    assert _all(db, "enriched__droplog", "sale_id, price, _duckstring_d") == [(1, 5, -1), (1, 50, 1)]
