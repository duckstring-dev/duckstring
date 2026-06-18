"""Trickle: incremental I/O via Z-sets / DBSP-style joins (plans/trickle-dbsp.md). Covers the append/merge
write paths, the ``_duckstring_d`` Z-set changelog, the windowed ``read_delta`` (consolidation / coverage
fallback / bootstrap / overwrite-source change detection), the DBSP builder (any-key joins, the fact+dim
both-change composition, deletes via full-row retraction, the comprehensive fallback, the change-fraction
threshold), retention, and the incremental draw window. Pinned to the Parquet plane (the window read is a
content predicate over ``_duckstring_f``, so it's plane-agnostic; Parquet is fast/offline)."""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane
from duckstring.engine.core import NEVER
from duckstring.local import hydrate, load_project, run_pond
from duckstring.trickle_builder import BuildError

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _parquet_plane(monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATA_PLANE", "parquet")


@pytest.fixture
def reg(tmp_path):
    """A persistent producer registry (so Trickle history accumulates across simulated runs)."""
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    yield con
    con.close()


def publish(con, data_dir, f=None):
    ParquetDataPlane().export(con, data_dir, f=f)
    return data_dir


def rows(con, data_dir, table, proj="*"):
    sql = ParquetDataPlane().read_select(data_dir, table)
    return sorted(con.sql(f"SELECT {proj} FROM ({sql})").fetchall())


def _state(con, pairs):
    if not pairs:
        return con.sql("SELECT CAST(NULL AS INTEGER) AS id, CAST(NULL AS VARCHAR) AS v WHERE 1=0")
    values = ", ".join(f"({i}, '{v}')" for i, v in pairs)
    return con.sql(f"SELECT * FROM (VALUES {values}) AS s(id, v)")


# ─── append ───────────────────────────────────────────────────────────────────


def test_append_accumulates_history_stamped_with_f(reg):
    T.append_table(reg, "event", reg.sql("SELECT 1 AS id, 'a' AS v"), ts(1), ("id",))
    T.append_table(reg, "event", reg.sql("SELECT 2 AS id, 'b' AS v"), ts(2), ("id",))
    assert sorted(reg.sql("SELECT id, v FROM event").fetchall()) == [(1, "a"), (2, "b")]


def test_append_idempotent_replay_at_same_f(reg):
    rel = reg.sql("SELECT 1 AS id, 'a' AS v")
    T.append_table(reg, "event", rel, ts(1), ("id",))
    T.append_table(reg, "event", rel, ts(1), ("id",))  # replay at the same freshness
    assert reg.sql("SELECT count(*) FROM event").fetchone()[0] == 1


def test_append_delta_window_is_plus_one(reg, tmp_path):
    for h in (1, 2, 3):
        T.append_table(reg, "event", reg.sql(f"SELECT {h} AS id"), ts(h), ("id",))
    data_dir = publish(reg, tmp_path / "data", f=ts(3))
    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    d = T.read_delta(rcon, data_dir, "event", previous_f=ts(1), f=ts(3), dp=ParquetDataPlane())
    assert sorted(d.zset.fetchall()) == [(2, 1), (3, 1)]  # rows 2,3 at weight +1 (append never retracts)
    assert not d.is_full


# ─── merge: the Z-set changelog ─────────────────────────────────────────────────


def test_merge_changelog_is_full_image_zset(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))           # bootstrap
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "B"), (3, "c")]), ts(2), ("id",))  # 2 upd, 3 ins
    T.merge_table(reg, "dim", _state(reg, [(2, "B")]), ts(3), ("id",))                       # 1,3 del

    data_dir = publish(reg, tmp_path / "data", f=ts(3))
    assert rows(reg, data_dir, "dim", "id, v") == [(2, "B")]  # clean main, no system columns
    clog = reg.sql(
        f"SELECT id, v, {T.D_COL}, {T.F_COL} FROM dim__changelog ORDER BY {T.F_COL}, id, {T.D_COL}"
    ).fetchall()
    assert clog == [
        # run 1 (bootstrap) writes NO changelog (consumers bootstrap from the main; floor marks below).
        (2, "b", -1, ts(2)), (2, "B", 1, ts(2)), (3, "c", 1, ts(2)),  # run 2: update 2 = -old +new; insert 3
        (1, "a", -1, ts(3)), (3, "c", -1, ts(3)),                      # run 3: delete 1 and 3 (retractions)
    ]
    assert T.load_sidecar(data_dir)["dim"]["floor"] == ts(1).isoformat()


def test_merge_main_is_pure_user_columns(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",))
    data_dir = publish(reg, tmp_path / "data", f=ts(1))
    full = duckdb.connect().sql(ParquetDataPlane().read_select(data_dir, "dim"))
    assert set(full.columns) == {"id", "v"}  # no _duckstring_* on the main


def test_merge_idempotent_replay(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))
    state2 = [(1, "a"), (2, "B")]
    T.merge_table(reg, "dim", _state(reg, state2), ts(2), ("id",))
    before = reg.sql("SELECT count(*) FROM dim__changelog").fetchone()[0]
    T.merge_table(reg, "dim", _state(reg, state2), ts(2), ("id",))  # replay: diff vs advanced main = empty
    after = reg.sql("SELECT count(*) FROM dim__changelog").fetchone()[0]
    assert before == after  # the empty-diff guard preserves the first attempt's changelog
    assert rows(reg, publish(reg, tmp_path / "d", f=ts(2)), "dim", "id, v") == [(1, "a"), (2, "B")]


def test_merge_window_consolidates_multiple_updates(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",))
    T.merge_table(reg, "dim", _state(reg, [(1, "b")]), ts(2), ("id",))  # a→b
    T.merge_table(reg, "dim", _state(reg, [(1, "c")]), ts(3), ("id",))  # b→c
    data_dir = publish(reg, tmp_path / "data", f=ts(3))
    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    # Window (f1, f3]: -a +b -b +c consolidates to -a +c (the intermediate b cancels).
    d = T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(3), dp=ParquetDataPlane())
    assert sorted(d.zset.fetchall()) == [(1, "a", -1), (1, "c", 1)]
    assert sorted(d.upserts.fetchall()) == [(1, "c")]
    assert d.deletes.fetchall() == []


def test_merge_delete_then_readd_resolves_present(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",))
    T.merge_table(reg, "dim", _state(reg, []), ts(2), ("id",))         # delete 1
    T.merge_table(reg, "dim", _state(reg, [(1, "z")]), ts(3), ("id",))  # re-add 1
    data_dir = publish(reg, tmp_path / "data", f=ts(3))
    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    d = T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(3), dp=ParquetDataPlane())
    assert sorted(d.upserts.fetchall()) == [(1, "z")]  # net: was 'a', now 'z'
    assert d.deletes.fetchall() == []


def test_merge_bootstrap_and_coverage_fallback_full_read(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(2), ("id",))
    data_dir = publish(reg, tmp_path / "data", f=ts(2))
    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    boot = T.read_delta(rcon, data_dir, "dim", previous_f=NEVER, f=ts(9), dp=ParquetDataPlane())
    assert boot.is_full and sorted(boot.upserts.fetchall()) == [(1, "a"), (2, "b")]
    miss = T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(9), dp=ParquetDataPlane())
    assert miss.is_full and sorted(miss.upserts.fetchall()) == [(1, "a"), (2, "b")]


def test_merge_requires_primary_key(reg):
    with pytest.raises(T.DeltaError):
        T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ())


# ─── builder DSL (pond.trickle) — DBSP composition ──────────────────────────────


def _producer(tmp_path, source, major=1):
    data_dir = tmp_path / "ponds" / source / f"m{major}" / "data"
    return duckdb.connect(str(tmp_path / f"{source}.duckdb")), data_dir


def _star_sources(tmp_path):
    ol_con, ol_dir = _producer(tmp_path, "sales")
    pr_con, pr_dir = _producer(tmp_path, "catalog")

    def ol(state, f):
        vals = ", ".join(f"({o}, '{p}', {q})" for o, p, q in state)
        T.merge_table(ol_con, "order_line",
                      ol_con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, qty)"), f, ("order_id",))
        publish(ol_con, ol_dir, f=f)

    def pr(state, f):
        vals = ", ".join(f"('{p}', {pr_})" for p, pr_ in state)
        T.merge_table(pr_con, "product",
                      pr_con.sql(f"SELECT * FROM (VALUES {vals}) v(product_id, price)"), f, ("product_id",))
        publish(pr_con, pr_dir, f=f)

    return (ol_con, pr_con), ol, pr


def _priced(tmp_path, f, previous_f, snk_con, snk_dir, *, spine_p=0.3, dim_p=0.3):
    pond = Pond("priced", "1.0.0", snk_con, root=tmp_path,
                source_majors={"sales": 1, "catalog": 1}, f=f, previous_f=previous_f)
    (pond.trickle("sales.order_line", p=spine_p)
         .join(pond.trickle("catalog.product", p=dim_p), on="product_id")
         .select("s0.order_id, s0.product_id, s0.qty, s1.price, s0.qty * s1.price AS total")
         .pk("order_id")
         .merge("priced"))
    publish(snk_con, snk_dir, f=f)


def test_builder_dimension_change_incremental(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"

    _priced(tmp_path, ts(1), NEVER, snk_con, snk_dir)  # bootstrap
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 10), (11, 9)]

    pr([("p1", 50), ("p2", 9)], ts(2))  # p1 price 5 → 50; only order 10 uses p1
    _priced(tmp_path, ts(2), ts(1), snk_con, snk_dir)
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 100), (11, 9)]
    snk_con.close()


def test_builder_key_prefilter_restricts_spine_on_dim_change(tmp_path):
    """The general-purpose performance lever: when a dimension changes, the (large) spine is pre-filtered
    to that dimension's affected join keys before the join — not a full spine scan. Guards the pushdown."""
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    con = duckdb.connect()
    pond = Pond("priced", "1.0.0", con, root=tmp_path,
                source_majors={"sales": 1, "catalog": 1}, f=ts(2), previous_f=ts(1))
    b = (pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id")
             .select("s0.order_id, s1.price").pk("order_id"))
    states = [b._state_views(r, pond.read_delta(r)) for r in ["sales.order_line", "catalog.product"]]
    dim_term = b._term(1, states)          # the dimension (catalog) is the delta
    assert "IN (SELECT" in dim_term         # spine pre-filtered to the dim's changed keys
    spine_term = b._term(0, states)         # the spine (sales) is the delta → no spine pre-filter
    assert "IN (SELECT" not in spine_term
    con.close()


def test_builder_propagates_spine_delete(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"
    _priced(tmp_path, ts(1), NEVER, snk_con, snk_dir)

    ol([(10, "p1", 2)], ts(2))  # order 11 removed from the spine
    _priced(tmp_path, ts(2), ts(1), snk_con, snk_dir)
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 10)]  # 11 dropped
    snk_con.close()


def test_builder_fact_and_dim_both_change(tmp_path):
    """The worked example (plans/trickle-dbsp.md): fact and dim both change in one run. The telescoping
    sum needs the dim's *old* state — the intermediate priced-at-old-price row must cancel."""
    f_con, f_dir = _producer(tmp_path, "fact")
    d_con, d_dir = _producer(tmp_path, "dim")
    T.merge_table(f_con, "f", f_con.sql("SELECT * FROM (VALUES (1,'A',10),(2,'A',5),(3,'B',7)) v(id,k,qty)"),
                  ts(1), ("id",))
    T.merge_table(d_con, "d", d_con.sql("SELECT * FROM (VALUES ('A',100),('B',200)) v(k,price)"), ts(1), ("k",))
    publish(f_con, f_dir, f=ts(1))
    publish(d_con, d_dir, f=ts(1))

    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "o" / "m1" / "data"

    def run(f, pf):
        p = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"fact": 1, "dim": 1}, f=f, previous_f=pf)
        (p.trickle("fact.f").join(p.trickle("dim.d"), on="k")
           .select("s0.id, s0.k, s0.qty, s1.price").pk("id").merge("o"))
        publish(snk, snk_dir, f=f)

    run(ts(1), NEVER)
    # run 2: fact updates id2 qty 5→8 and inserts id4; dim updates A price 100→120.
    T.merge_table(f_con, "f", f_con.sql("SELECT * FROM (VALUES (1,'A',10),(2,'A',8),(3,'B',7),(4,'B',2)) v(id,k,qty)"),
                  ts(2), ("id",))
    T.merge_table(d_con, "d", d_con.sql("SELECT * FROM (VALUES ('A',120),('B',200)) v(k,price)"), ts(2), ("k",))
    publish(f_con, f_dir, f=ts(2))
    publish(d_con, d_dir, f=ts(2))
    run(ts(2), ts(1))

    assert rows(snk, snk_dir, "o", "id, k, qty, price") == [
        (1, "A", 10, 120), (2, "A", 8, 120), (3, "B", 7, 200), (4, "B", 2, 200),
    ]
    snk.close()


def test_builder_matches_comprehensive_row_for_row(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"
    _priced(tmp_path, ts(1), NEVER, snk_con, snk_dir)
    pr([("p1", 50), ("p2", 9)], ts(2))
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3), (13, "p2", 4)], ts(2))  # add order 13
    _priced(tmp_path, ts(2), ts(1), snk_con, snk_dir)

    truth_con = duckdb.connect()
    truth_con.execute("SET TimeZone='UTC'")
    ol_main = T._strip_system(truth_con.sql(ParquetDataPlane().read_select(tmp_path / "ponds/sales/m1/data", "order_line")))
    pr_main = T._strip_system(truth_con.sql(ParquetDataPlane().read_select(tmp_path / "ponds/catalog/m1/data", "product")))
    ol_main.create_view("ol", replace=True)
    pr_main.create_view("pr", replace=True)
    truth = sorted(truth_con.sql(
        "SELECT ol.order_id, ol.qty * pr.price FROM ol JOIN pr USING (product_id) ORDER BY 1"
    ).fetchall())
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == truth
    snk_con.close()


def test_builder_joins_on_non_pk_business_code(tmp_path):
    """The headline capability: join on any column (a business code), not the dimension's PK — and a
    deletion still propagates (it is a full-row retraction, not a key-only tombstone)."""
    f_con, f_dir = _producer(tmp_path, "fact")
    d_con, d_dir = _producer(tmp_path, "dim")
    # fact joins to dim on `code`; dim's own PK is `sku`, NOT `code`.
    T.merge_table(f_con, "f", f_con.sql("SELECT * FROM (VALUES (1,'X'),(2,'Y'),(3,'X')) v(id,code)"), ts(1), ("id",))
    T.merge_table(d_con, "d", d_con.sql("SELECT * FROM (VALUES ('s1','X','xan'),('s2','Y','yur')) v(sku,code,label)"),
                  ts(1), ("sku",))
    publish(f_con, f_dir, f=ts(1))
    publish(d_con, d_dir, f=ts(1))
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "o" / "m1" / "data"

    def run(f, pf):
        p = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"fact": 1, "dim": 1}, f=f, previous_f=pf)
        (p.trickle("fact.f").join(p.trickle("dim.d"), on="code")
           .select("s0.id, s0.code, s1.label").pk("id").merge("o"))
        publish(snk, snk_dir, f=f)

    run(ts(1), NEVER)
    assert rows(snk, snk_dir, "o", "id, label") == [(1, "xan"), (2, "yur"), (3, "xan")]
    # the dim row for code Y is deleted (by its sku PK) → output rows joining Y must drop.
    T.merge_table(d_con, "d", d_con.sql("SELECT * FROM (VALUES ('s1','X','xan')) v(sku,code,label)"), ts(2), ("sku",))
    publish(d_con, d_dir, f=ts(2))
    run(ts(2), ts(1))
    assert rows(snk, snk_dir, "o", "id, label") == [(1, "xan"), (3, "xan")]  # id 2 (code Y) dropped
    snk.close()


# ─── ripple (overwrite) inputs ───────────────────────────────────────────────────


def test_builder_unchanged_ripple_is_stable_history(tmp_path, monkeypatch):
    """An unchanged overwrite Ripple dimension (its published f hasn't advanced) is a free stable operand:
    the fact's delta flows incrementally, no comprehensive recompute."""
    f_con, f_dir = _producer(tmp_path, "fact")
    d_con, d_dir = _producer(tmp_path, "dim")
    T.merge_table(f_con, "f", f_con.sql("SELECT * FROM (VALUES (1,'X'),(2,'Y')) v(id,code)"), ts(1), ("id",))
    d_con.execute("CREATE TABLE m AS SELECT * FROM (VALUES ('X','xan'),('Y','yur')) v(code,label)")
    publish(f_con, f_dir, f=ts(1))
    publish(d_con, d_dir, f=ts(1))  # plain overwrite dim
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "o" / "m1" / "data"
    paths = _spy_paths(monkeypatch)

    def run(f, pf):
        p = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"fact": 1, "dim": 1}, f=f, previous_f=pf)
        (p.trickle("fact.f", p=1.0).join(p.trickle("dim.m", p=1.0), on="code")
           .select("s0.id, s0.code, s1.label").pk("id").merge("o"))
        publish(snk, snk_dir, f=f)

    run(ts(1), NEVER)
    paths.clear()
    # fact adds id 3; dim re-published at the SAME freshness → unchanged → incremental.
    T.merge_table(f_con, "f", f_con.sql("SELECT * FROM (VALUES (1,'X'),(2,'Y'),(3,'X')) v(id,code)"), ts(2), ("id",))
    publish(f_con, f_dir, f=ts(2))
    publish(d_con, d_dir, f=ts(1))  # dim NOT advanced
    run(ts(2), ts(1))
    assert paths == ["incremental"]
    assert rows(snk, snk_dir, "o", "id, label") == [(1, "xan"), (2, "yur"), (3, "xan")]
    snk.close()


def test_builder_changed_ripple_forces_comprehensive(tmp_path, monkeypatch):
    f_con, f_dir = _producer(tmp_path, "fact")
    d_con, d_dir = _producer(tmp_path, "dim")
    T.merge_table(f_con, "f", f_con.sql("SELECT * FROM (VALUES (1,'X'),(2,'Y')) v(id,code)"), ts(1), ("id",))
    d_con.execute("CREATE TABLE m AS SELECT * FROM (VALUES ('X','xan'),('Y','yur')) v(code,label)")
    publish(f_con, f_dir, f=ts(1))
    publish(d_con, d_dir, f=ts(1))
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "o" / "m1" / "data"
    paths = _spy_paths(monkeypatch)

    def run(f, pf):
        p = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"fact": 1, "dim": 1}, f=f, previous_f=pf)
        (p.trickle("fact.f", p=1.0).join(p.trickle("dim.m", p=1.0), on="code")
           .select("s0.id, s0.code, s1.label").pk("id").merge("o"))
        publish(snk, snk_dir, f=f)

    run(ts(1), NEVER)
    paths.clear()
    # dim ripple changes (relabel X, drop Y) AND advances its freshness → comprehensive recompute.
    d_con.execute("CREATE OR REPLACE TABLE m AS SELECT * FROM (VALUES ('X','XAN')) v(code,label)")
    publish(d_con, d_dir, f=ts(2))
    run(ts(2), ts(1))
    assert paths == ["comprehensive"]
    assert rows(snk, snk_dir, "o", "id, label") == [(1, "XAN")]  # id 2 (code Y) dropped, X relabelled
    snk.close()


# ─── change-fraction threshold ───────────────────────────────────────────────────


def _spy_paths(monkeypatch):
    """Record which path the builder took per run: 'comprehensive' (Pond.merge_table) vs 'incremental'
    (Pond.apply_zset)."""
    paths: list[str] = []
    orig_m, orig_z = Pond.merge_table, Pond.apply_zset

    def spy_m(self, *a, **k):
        paths.append("comprehensive")
        return orig_m(self, *a, **k)

    def spy_z(self, *a, **k):
        paths.append("incremental")
        return orig_z(self, *a, **k)

    monkeypatch.setattr(Pond, "merge_table", spy_m)
    monkeypatch.setattr(Pond, "apply_zset", spy_z)
    return paths


def test_builder_threshold_falls_back_to_comprehensive(tmp_path, monkeypatch):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p3", 3)], ts(1))
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"
    _priced(tmp_path, ts(1), NEVER, snk_con, snk_dir)

    paths = _spy_paths(monkeypatch)
    pr([("p1", 50), ("p2", 90), ("p3", 70)], ts(2))  # 3 of 3 prices change → 100% > p=0.3
    _priced(tmp_path, ts(2), ts(1), snk_con, snk_dir)
    assert paths == ["comprehensive"]
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 100), (11, 90), (12, 210)]
    snk_con.close()


def test_builder_threshold_p1_forces_incremental(tmp_path, monkeypatch):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p3", 3)], ts(1))
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"
    _priced(tmp_path, ts(1), NEVER, snk_con, snk_dir, dim_p=1.0)

    paths = _spy_paths(monkeypatch)
    pr([("p1", 50), ("p2", 90), ("p3", 70)], ts(2))  # 100% churn, but dim_p=1.0 → stays incremental
    _priced(tmp_path, ts(2), ts(1), snk_con, snk_dir, dim_p=1.0)
    assert paths == ["incremental"]
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 100), (11, 90), (12, 210)]
    snk_con.close()


# ─── build-time errors ───────────────────────────────────────────────────────────


def test_builder_build_time_errors(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    con = duckdb.connect()
    pond = Pond("priced", "1.0.0", con, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=ts(1))

    # A snowflake (a dimension that itself has joins) is outside the op set.
    snowflake_dim = pond.trickle("catalog.product").join(pond.trickle("catalog.product"), on="product_id")
    with pytest.raises(BuildError, match="snowflake|deeper hop"):
        pond.trickle("sales.order_line").join(snowflake_dim, on="product_id")

    # A joined graph with no .select(...).
    with pytest.raises(BuildError, match="select"):
        pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id").pk("order_id").merge("x")

    # No .pk(...).
    with pytest.raises(BuildError, match="pk"):
        (pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id")
             .select("s0.order_id, s1.price").merge("x"))

    # .select that omits the PK.
    with pytest.raises(BuildError, match="PK"):
        (pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id")
             .select("s1.price").pk("order_id").merge("x"))


# ─── coverage-miss absorption (the retention-lag delete bug) ─────────────────────


def test_builder_absorbs_coverage_miss_comprehensively(tmp_path):
    ol_con, ol_dir = _producer(tmp_path, "sales")
    pr_con, pr_dir = _producer(tmp_path, "catalog")

    def order_lines(state, f):
        vals = ", ".join(f"({o}, '{p}')" for o, p in state)
        T.merge_table(ol_con, "order_line",
                      ol_con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id)"), f, ("order_id",), retain_n=1)
        publish(ol_con, ol_dir, f=f)

    T.merge_table(pr_con, "product", pr_con.sql("SELECT * FROM (VALUES ('p1', 5), ('p2', 9)) v(product_id, price)"),
                  ts(1), ("product_id",))
    publish(pr_con, pr_dir, f=ts(1))
    order_lines([(10, "p1"), (11, "p2")], ts(1))

    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"

    def build(f, previous_f):
        pond = Pond("priced", "1.0.0", snk, root=tmp_path,
                    source_majors={"sales": 1, "catalog": 1}, f=f, previous_f=previous_f)
        (pond.trickle("sales.order_line")
             .join(pond.trickle("catalog.product"), on="product_id")
             .select("s0.order_id, s0.product_id, s1.price").pk("order_id").merge("priced"))
        publish(snk, snk_dir, f=f)

    build(ts(1), NEVER)
    assert rows(snk, snk_dir, "priced", "order_id") == [(10,), (11,)]
    order_lines([(10, "p1")], ts(2))   # 11 removed
    order_lines([(10, "p1")], ts(3))   # advance again so the changelog floor passes ts(1)
    build(ts(3), ts(1))
    assert rows(snk, snk_dir, "priced", "order_id") == [(10,)]  # 11 dropped, not stale
    snk.close()


# ─── retention (lag SLA; correctness never depends on it) ────────────────────────


def test_retention_retain_n_keeps_newest_runs(reg):
    for h, v in [(1, "a"), (2, "b"), (3, "c"), (4, "d")]:
        T.merge_table(reg, "dim", _state(reg, [(1, v)]), ts(h), ("id",), retain_n=2)
    kept = [r[0] for r in reg.sql(f"SELECT DISTINCT {T.F_COL} FROM dim__changelog ORDER BY 1").fetchall()]
    assert kept == [ts(3), ts(4)]


def test_retention_retain_t_drops_old(reg):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), retain_t=timedelta(hours=2))
    T.merge_table(reg, "dim", _state(reg, [(1, "b")]), ts(4), ("id",), retain_t=timedelta(hours=2))
    kept = [r[0] for r in reg.sql(f"SELECT DISTINCT {T.F_COL} FROM dim__changelog ORDER BY 1").fetchall()]
    assert kept == [ts(4)]


def test_append_retention_and_window(reg):
    for h in (1, 2, 3):
        T.append_table(reg, "event", reg.sql(f"SELECT {h} AS id"), ts(h), ("id",), retain_n=2)
    assert sorted(r[0] for r in reg.sql("SELECT id FROM event").fetchall()) == [2, 3]


def test_floor_anchors_at_bootstrap_and_retention_advances_it(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",))
    assert T.load_sidecar(publish(reg, tmp_path / "d1", f=ts(1)))["dim"]["floor"] == ts(1).isoformat()
    T.merge_table(reg, "dim", _state(reg, [(1, "b")]), ts(2), ("id",))
    assert T.load_sidecar(publish(reg, tmp_path / "d2", f=ts(2)))["dim"]["floor"] == ts(1).isoformat()
    T.merge_table(reg, "dim", _state(reg, [(1, "c")]), ts(3), ("id",), retain_n=1)
    assert T.load_sidecar(publish(reg, tmp_path / "d3", f=ts(3)))["dim"]["floor"] == ts(3).isoformat()


# ─── @trickle decorator threading through the local runner (pk default) ──────────


def _trickle_project(tmp_path: Path) -> Path:
    (tmp_path / "pond.toml").write_text(
        '[pond]\nname = "loud"\nversion = "0.1.0"\n[sources]\nsrc = "1.0.0"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pond.py").write_text(textwrap.dedent("""
        from duckstring import trickle

        @trickle(pk="id")                       # the PK default the merge inherits (no pk= at the call)
        def loud(pond):
            pond.read_table("src.dim")          # registers the Source as the view `dim`
            pond.merge_table("loud", pond.con.sql("SELECT id, upper(v) AS v FROM dim"))
    """))
    (tmp_path / "src" / "puddles.py").write_text(textwrap.dedent("""
        from duckstring import puddle

        @puddle("src.dim")
        def src_dim(p):
            return p.con.sql("SELECT 1 AS id, 'a' AS v UNION ALL SELECT 2 AS id, 'b' AS v")
    """))
    return tmp_path


def test_trickle_decorator_pk_default_via_local_runner(tmp_path):
    project = load_project(_trickle_project(tmp_path))
    hydrate(project)
    result = run_pond(project)
    assert result.ok, [r.error for r in result.ripples if r.status != "ok"]

    out = project.out_dir
    _sc = T.load_sidecar(out)["loud"]
    assert _sc["mode"] == "merge" and _sc["pk"] == ["id"] and _sc["floor"] is not None
    con = duckdb.connect()
    assert rows(con, out, "loud", "id, v") == [(1, "A"), (2, "B")]
    assert con.sql(
        f"SELECT count(*) FROM ({ParquetDataPlane().read_select(out, 'loud__changelog')})"
    ).fetchone()[0] == 0  # bootstrap → empty changelog


# ─── incremental draw (cross-Catchment transfer window) ──────────────────────────


def test_incremental_draw_window_roundtrip(tmp_path):
    import shutil

    prod, cons = tmp_path / "prod", tmp_path / "cons"
    con = duckdb.connect()

    def producer_run(pairs, hour):
        vals = ", ".join(f"({i}, '{v}')" for i, v in pairs)
        T.merge_table(con, "dim", con.sql(f"SELECT * FROM (VALUES {vals}) t(id, v)"), ts(hour), ("id",))
        ParquetDataPlane().export(con, prod, f=ts(hour))

    producer_run([(1, "a"), (2, "b")], 1)
    producer_run([(1, "A"), (3, "c")], 2)
    cons.mkdir()
    for f in prod.iterdir():
        shutil.copy(f, cons / f.name)
    assert T.landed_after(cons) == ts(2).isoformat()

    producer_run([(1, "A"), (3, "C"), (4, "d")], 3)

    after = T.landed_after(cons)
    shipped = T.window_parquet_bytes(prod / "dim__changelog.parquet", after)
    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    (tmp_path / "shipped.parquet").write_bytes(shipped)
    assert rcon.sql(
        f"SELECT DISTINCT {T.F_COL} FROM read_parquet('{tmp_path / 'shipped.parquet'}')"
    ).fetchall() == [(ts(3),)]

    T.land_windowed(cons / "dim__changelog.parquet", shipped, after)
    shutil.copy(prod / "dim.parquet", cons / "dim.parquet")

    d = T.read_delta(rcon, cons, "dim", previous_f=ts(2), f=ts(3), dp=ParquetDataPlane())
    assert sorted(d.upserts.fetchall()) == [(3, "C"), (4, "d")]
    assert d.deletes.fetchall() == []


def test_landed_after_bootstrap_is_none(tmp_path):
    assert T.landed_after(tmp_path) is None
