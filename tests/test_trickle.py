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


def test_append_fail_on_conflict_catches_duplicates(reg):
    # fail_on_conflict defaults True; with a pk it asserts uniqueness. Duplicate within the appended batch:
    with pytest.raises(T.DeltaError, match="duplicate"):
        T.append_table(reg, "event", reg.sql("SELECT 1 AS id UNION ALL SELECT 1 AS id"), ts(1), ("id",))
    assert not reg.sql("SELECT 1 FROM information_schema.tables WHERE table_name = 'event'").fetchall()

    # Collision against existing history (a different run's row).
    T.append_table(reg, "event", reg.sql("SELECT 1 AS id"), ts(1), ("id",))
    with pytest.raises(T.DeltaError, match="already present"):
        T.append_table(reg, "event", reg.sql("SELECT 1 AS id"), ts(2), ("id",))

    # A replay at the same f re-appends its own rows without tripping the check.
    T.append_table(reg, "event", reg.sql("SELECT 1 AS id"), ts(1), ("id",))
    assert reg.sql("SELECT count(*) FROM event").fetchone()[0] == 1

    # fail_on_conflict=False is the trust-the-writer fast path — a colliding pk is appended without a check.
    T.append_table(reg, "event", reg.sql("SELECT 1 AS id"), ts(3), ("id",), fail_on_conflict=False)
    assert reg.sql("SELECT count(*) FROM event").fetchone()[0] == 2


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


class _CrashOn:
    """Proxy a DuckDB connection, raising when an executed statement contains ``needle`` — to inject a
    crash mid-apply and prove the changelog + main commit/abort atomically."""

    def __init__(self, con, needle):
        self._c, self._needle = con, needle

    def execute(self, sql, *a, **k):
        if self._needle in sql:
            raise RuntimeError("injected crash")
        return self._c.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_merge_apply_is_atomic_changelog_and_main(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",))  # bootstrap
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (2, "b")]), ts(2), ("id",))  # changelog now exists

    # Crash on the main CoW insert — after the changelog rows were inserted in the same transaction.
    with pytest.raises(RuntimeError):
        T.merge_table(_CrashOn(reg, 'INSERT INTO "dim" SELECT'),
                      "dim", _state(reg, [(1, "Z"), (2, "b")]), ts(3), ("id",))

    # The transaction rolled back: main is untouched AND the changelog has no ts(3) rows (never the
    # main-advanced / changelog-missing state the old ordering could produce).
    assert sorted(reg.execute("SELECT id, v FROM dim").fetchall()) == [(1, "A"), (2, "b")]
    assert reg.execute(
        f"SELECT count(*) FROM dim__changelog WHERE {T.F_COL} = {T._ts(ts(3))}"
    ).fetchone()[0] == 0

    # A clean replay recovers fully — both the main and the changelog land.
    T.merge_table(reg, "dim", _state(reg, [(1, "Z"), (2, "b")]), ts(3), ("id",))
    assert sorted(reg.execute("SELECT id, v FROM dim").fetchall()) == [(1, "Z"), (2, "b")]
    assert sorted(reg.execute(
        f"SELECT id, v, {T.D_COL} FROM dim__changelog WHERE {T.F_COL} = {T._ts(ts(3))}"
    ).fetchall()) == [(1, "A", -1), (1, "Z", 1)]


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
         .merge("priced", pk="order_id"))
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
             .select("s0.order_id, s1.price"))
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
           .select("s0.id, s0.k, s0.qty, s1.price").merge("o", pk="id"))
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
           .select("s0.id, s0.code, s1.label").merge("o", pk="id"))
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
           .select("s0.id, s0.code, s1.label").merge("o", pk="id"))
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
           .select("s0.id, s0.code, s1.label").merge("o", pk="id"))
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
        pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id").merge("x", pk="order_id")

    # No pk passed to .merge(...) — pk is a required keyword arg (TypeError, like merge_table).
    with pytest.raises(TypeError, match="pk"):
        (pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id")
             .select("s0.order_id, s1.price").merge("x"))

    # An empty/None pk passed explicitly raises a friendly BuildError.
    with pytest.raises(BuildError, match="key"):
        (pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id")
             .select("s0.order_id, s1.price").merge("x", pk=()))

    # .select that omits the PK.
    with pytest.raises(BuildError, match="PK"):
        (pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id")
             .select("s1.price").merge("x", pk="order_id"))


# ─── chained merges: materialise an intermediate mid-chain in one ripple ─────────


def _chain_sources(tmp_path):
    """Three producers for a genuine *chain* a→b→c (c joins on a column produced by a⋈b, not on the spine —
    a shape a single star builder can't express, so the intermediate must be materialised)."""
    a_con, a_dir = _producer(tmp_path, "a")
    b_con, b_dir = _producer(tmp_path, "b")
    c_con, c_dir = _producer(tmp_path, "c")

    def a(state, f):  # order lines: order_id → product_id
        vals = ", ".join(f"({o}, '{p}', {q})" for o, p, q in state)
        T.merge_table(a_con, "ol", a_con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, qty)"),
                      f, ("order_id",))
        publish(a_con, a_dir, f=f)

    def b(state, f):  # product → category
        vals = ", ".join(f"('{p}', '{cat}')" for p, cat in state)
        T.merge_table(b_con, "prod", b_con.sql(f"SELECT * FROM (VALUES {vals}) v(product_id, category)"),
                      f, ("product_id",))
        publish(b_con, b_dir, f=f)

    def c(state, f):  # category → tax (the join key only exists after a⋈b)
        vals = ", ".join(f"('{cat}', {t})" for cat, t in state)
        T.merge_table(c_con, "tax", c_con.sql(f"SELECT * FROM (VALUES {vals}) v(category, tax)"), f, ("category",))
        publish(c_con, c_dir, f=f)

    return (a_con, b_con, c_con), a, b, c


def test_builder_chains_through_materialised_intermediate(tmp_path):
    _cons, a, b, c = _chain_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    out_dir = tmp_path / "ponds" / "o" / "m1" / "data"

    def run(f, pf):
        p = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"a": 1, "b": 1, "c": 1}, f=f, previous_f=pf)
        ab = (p.trickle("a.ol").join(p.trickle("b.prod"), on="product_id")
                .select("s0.order_id, s0.product_id, s0.qty, s1.category").merge("ab", pk="order_id"))
        (ab.join(p.trickle("c.tax"), on="category")
            .select("s0.order_id, s0.qty, s0.category, s1.tax").merge("abc", pk="order_id"))
        publish(snk, out_dir, f=f)

    # bootstrap — the is_full of a/b cascades through ab into abc (comprehensive both hops).
    a([(10, "p1", 2), (11, "p2", 1)], ts(1))
    b([("p1", "books"), ("p2", "food")], ts(1))
    c([("books", 10), ("food", 5)], ts(1))
    run(ts(1), NEVER)
    assert rows(snk, out_dir, "abc", "order_id, category, tax") == [(10, "books", 10), (11, "food", 5)]
    assert rows(snk, out_dir, "ab", "order_id, category") == [(10, "books"), (11, "food")]  # intermediate stored

    # c-only change (books tax 10→20): ab is unchanged (not recomputed); only order 10 (books) flows to abc.
    c([("books", 20), ("food", 5)], ts(2))
    run(ts(2), ts(1))
    assert rows(snk, out_dir, "ab", "order_id, category") == [(10, "books"), (11, "food")]  # ab untouched
    assert rows(snk, out_dir, "abc", "order_id, category, tax") == [(10, "books", 20), (11, "food", 5)]

    # a-only change (order 11's product p2→p1, i.e. food→books): the change propagates through the stored ab.
    a([(10, "p1", 2), (11, "p1", 1)], ts(3))
    run(ts(3), ts(2))
    assert rows(snk, out_dir, "ab", "order_id, category") == [(10, "books"), (11, "books")]
    assert rows(snk, out_dir, "abc", "order_id, category, tax") == [(10, "books", 20), (11, "books", 20)]
    snk.close()


def test_builder_chain_matches_comprehensive_join(tmp_path):
    """A chained build equals the full 3-way join recomputed from scratch (correctness of the threaded
    in-run delta), across an incremental run."""
    _cons, a, b, c = _chain_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    out_dir = tmp_path / "ponds" / "o" / "m1" / "data"

    def run(f, pf):
        p = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"a": 1, "b": 1, "c": 1}, f=f, previous_f=pf)
        ab = (p.trickle("a.ol").join(p.trickle("b.prod"), on="product_id")
                .select("s0.order_id, s0.product_id, s0.qty, s1.category").merge("ab", pk="order_id"))
        (ab.join(p.trickle("c.tax"), on="category")
            .select("s0.order_id, s0.qty, s0.category, s1.tax").merge("abc", pk="order_id"))
        publish(snk, out_dir, f=f)

    a([(10, "p1", 2), (11, "p2", 1), (12, "p3", 4)], ts(1))
    b([("p1", "books"), ("p2", "food"), ("p3", "books")], ts(1))
    c([("books", 10), ("food", 5)], ts(1))
    run(ts(1), NEVER)
    # An incremental run touching all three sources at once.
    a([(10, "p1", 2), (11, "p2", 1), (12, "p3", 9), (13, "p1", 1)], ts(2))  # 12 qty change, 13 new
    b([("p1", "media"), ("p2", "food"), ("p3", "books")], ts(2))            # p1 books→media
    c([("books", 10), ("food", 7), ("media", 3)], ts(2))                    # food tax change, media new
    run(ts(2), ts(1))

    got = rows(snk, out_dir, "abc", "order_id, category, tax")
    # The ground truth: recompute the whole 3-way join from the published source mains.
    ref = duckdb.connect()
    want = sorted(ref.sql(f"""
        SELECT ol.order_id, prod.category, tax.tax
        FROM ({ParquetDataPlane().read_select(tmp_path / 'ponds/a/m1/data', 'ol')}) ol
        JOIN ({ParquetDataPlane().read_select(tmp_path / 'ponds/b/m1/data', 'prod')}) prod USING (product_id)
        JOIN ({ParquetDataPlane().read_select(tmp_path / 'ponds/c/m1/data', 'tax')}) tax USING (category)
    """).fetchall())
    assert got == want
    snk.close()


# ─── builder .append() terminal (monotonic transforms + conflict handling) ───────


def _enriched(tmp_path, snk, snk_dir, f, pf, *, fail_on_conflict=True, log_drops=True, dim_p=0.3):
    """Append-enrich the order-line spine with the product dim (one output row per order_id). ``dim_p=1.0``
    forces the incremental path (skips the change-fraction threshold) so a tiny dim drives a Z-set delta."""
    pond = Pond("e", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=f, previous_f=pf)
    (pond.trickle("sales.order_line").join(pond.trickle("catalog.product", p=dim_p), on="product_id")
         .select("s0.order_id, s0.product_id, s0.qty, s1.price")
         .append("enriched", pk="order_id", fail_on_conflict=fail_on_conflict, log_drops=log_drops))
    publish(snk, snk_dir, f=f)


def test_builder_append_monotonic_accumulates_history(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "e" / "m1" / "data"

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _enriched(tmp_path, snk, snk_dir, ts(1), NEVER)               # bootstrap → append both
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9)]
    assert T.load_sidecar(snk_dir)["enriched"]["mode"] == "append"

    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(2))     # a NEW order (monotonic spine growth)
    _enriched(tmp_path, snk, snk_dir, ts(2), ts(1))
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 5)]
    snk.close()


def test_builder_append_fails_on_changed_past(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "e" / "m1" / "data"

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _enriched(tmp_path, snk, snk_dir, ts(1), NEVER)

    pr([("p1", 50), ("p2", 9)], ts(2))   # p1 reprice retracts order 10's enriched row — not append-safe
    with pytest.raises(T.DeltaError, match="not append-safe|retraction|changed-past"):
        _enriched(tmp_path, snk, snk_dir, ts(2), ts(1))
    snk.close()


def test_builder_append_drops_conflicts_and_logs(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "e" / "m1" / "data"

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    _enriched(tmp_path, snk, snk_dir, ts(1), NEVER)

    # p1 reprice, forced down the incremental path (dim_p=1.0) so ΔO carries the retraction explicitly.
    pr([("p1", 50), ("p2", 9)], ts(2))
    _enriched(tmp_path, snk, snk_dir, ts(2), ts(1), fail_on_conflict=False, dim_p=1.0)
    # Order 10 stays frozen at its original enriched price (the past is not rewritten).
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9)]
    # Both dropped rows are logged to the __droplog companion: the retraction (−1, old price 5) and the
    # changed-image insert (+1, new price 50).
    drops = snk.sql('SELECT order_id, price, _duckstring_d FROM "enriched__droplog" ORDER BY _duckstring_d').fetchall()
    assert drops == [(10, 5, -1), (10, 50, 1)]
    # ...and the __droplog is published alongside the table (like __changelog) — readable, but not a Trickle
    # base, so it carries no sidecar entry.
    published = ParquetDataPlane().read_select(snk_dir, "enriched__droplog")
    assert sorted(snk.sql(f"SELECT order_id, price, _duckstring_d FROM ({published})").fetchall()) == [
        (10, 5, -1), (10, 50, 1),
    ]
    assert "enriched__droplog" not in T.load_sidecar(snk_dir)

    # A new order still flows through while the conflict is tolerated.
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p2", 4)], ts(3))
    _enriched(tmp_path, snk, snk_dir, ts(3), ts(2), fail_on_conflict=False)
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 9)]
    snk.close()


def test_builder_append_log_drops_false_squashes_table(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "e" / "m1" / "data"

    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    _enriched(tmp_path, snk, snk_dir, ts(1), NEVER, fail_on_conflict=False, log_drops=False)
    pr([("p1", 50)], ts(2))
    _enriched(tmp_path, snk, snk_dir, ts(2), ts(1), fail_on_conflict=False, log_drops=False)
    assert not _table_exists(snk, "enriched__droplog")
    snk.close()


def _table_exists(con, name):
    return con.execute("SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [name]).fetchone() is not None


# ─── spine-PK append fast path (skip dim deltas when output is keyed by the spine) ───


def test_spine_pk_passthrough_detection():
    from duckstring.trickle_builder import TrickleBuilder

    def detect(projection, out_pk, spine_pk, spine_alias=None):
        b = TrickleBuilder(None, "spine")
        b._projection = projection
        b._alias = spine_alias
        return b._spine_pk_passthrough(out_pk, spine_pk)

    # Verbatim s0 pass-throughs (single, renamed, composite) → detected.
    assert detect("s0.order_id, s1.price", ("order_id",), ("order_id",)) == {"order_id": "order_id"}
    # ...and the same off a custom spine .alias() (the detector keys on the spine's effective alias).
    assert detect("o.order_id, p.price", ("order_id",), ("order_id",), spine_alias="o") == {"order_id": "order_id"}
    assert detect("s0.order_id, p.price", ("order_id",), ("order_id",), spine_alias="o") is None  # s0 isn't the alias
    assert detect("s0.oid AS order_id, s1.price", ("order_id",), ("oid",)) == {"order_id": "oid"}
    assert detect('s0."a", s0.b, s1.x', ("a", "b"), ("a", "b")) == {"a": "a", "b": "b"}
    # A comma inside a computed dim column doesn't break item splitting.
    assert detect("s0.id, round(s1.a, 2) AS r", ("id",), ("id",)) == {"id": "id"}

    # Conservative bails (→ None, falls back to the always-correct general path):
    assert detect("round(s0.x, 2) AS k, s1.y", ("k",), ("x",)) is None    # computed PK
    assert detect("s1.k, s0.v", ("k",), ("k",)) is None                    # PK from a dimension, not s0
    assert detect("s0.order_id, s0.region", ("order_id", "region"), ("order_id",)) is None  # out PK ⊋ spine PK
    assert detect("s0.order_id", ("order_id",), ()) is None                # spine has no declared PK


def test_builder_append_spine_pk_fast_path(tmp_path, monkeypatch):
    from duckstring.trickle_builder import TrickleBuilder

    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "e" / "m1" / "data"

    def run(f, pf):  # fail_on_conflict=False + log_drops=False + s0.order_id PK → fast path eligible
        _enriched(tmp_path, snk, snk_dir, f, pf, fail_on_conflict=False, log_drops=False)

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9)]

    # A dimension-only change touches no spine rows → nothing flows; the past stays frozen.
    pr([("p1", 50), ("p2", 9)], ts(2))
    run(ts(2), ts(1))
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9)]

    # A new spine row arriving the same run a dim reprices is enriched with the *current* dim value.
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(3))
    pr([("p1", 70), ("p2", 9)], ts(3))
    run(ts(3), ts(2))
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 70)]

    # Prove the fast path skipped the telescoping ΔO composition entirely: with _compute sabotaged, a
    # new-spine-row run still succeeds (it never calls it).
    def _boom(*a, **k):
        raise AssertionError("_compute should not run on the spine-PK fast path")

    monkeypatch.setattr(TrickleBuilder, "_compute", _boom)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3), (13, "p2", 1)], ts(4))
    run(ts(4), ts(3))
    assert rows(snk, snk_dir, "enriched", "order_id, price") == [(10, 5), (11, 9), (12, 70), (13, 9)]
    snk.close()


# ─── .alias() / .sql() / .schema() (relational surface, Ibis-aligned) ────────────


def test_builder_alias_names_sources(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "o" / "m1" / "data"

    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    pond = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=ts(1), previous_f=NEVER)
    (pond.trickle("sales.order_line").alias("ol")
         .join(pond.trickle("catalog.product").alias("pr"), on="product_id")
         .select("ol.order_id, ol.qty, pr.price, ol.qty * pr.price AS total")
         .merge("priced", pk="order_id"))
    publish(snk, snk_dir, f=ts(1))
    assert rows(snk, snk_dir, "priced", "order_id, total") == [(10, 10), (11, 9)]
    snk.close()


def test_builder_sql_aggregation_keeps_incremental_output(tmp_path):
    """The priced→revenue pattern in one ripple: incremental join cached with .merge(), aggregated with the
    comprehensive .sql() escape, final .merge() — and the delta OUT stays incremental (only changed groups)."""
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "rev" / "m1" / "data"

    def run(f, pf):
        pond = Pond("rev", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=f, previous_f=pf)
        priced = (
            pond.trickle("sales.order_line").alias("o")
                .join(pond.trickle("catalog.product").alias("p"), on="product_id")
                .select("o.order_id, o.product_id, o.qty * p.price AS revenue")
                .merge("priced_line", pk="order_id")
        )
        agg = priced.alias("pl").sql(
            "SELECT product_id, sum(revenue) AS total_revenue, count(*) AS n FROM pl GROUP BY product_id"
        )
        with pytest.raises(BuildError, match="after .sql"):
            agg.join(pond.trickle("catalog.product"), on="product_id")   # no incremental ops post-.sql
        agg.merge("revenue_by_product", pk="product_id")
        publish(snk, snk_dir, f=f)

    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    run(ts(1), NEVER)
    # p1: 2*5 + 3*5 = 25 (n=2); p2: 1*9 = 9 (n=1).
    assert rows(snk, snk_dir, "revenue_by_product", "product_id, total_revenue, n") == [("p1", 25, 2), ("p2", 9, 1)]

    pr([("p1", 10), ("p2", 9)], ts(2))   # only p1 reprices
    run(ts(2), ts(1))
    assert rows(snk, snk_dir, "revenue_by_product", "product_id, total_revenue, n") == [("p1", 50, 2), ("p2", 9, 1)]
    # Incremental output: only p1 moved → only p1 in this run's changelog window (p2 untouched).
    latest = snk.sql('SELECT DISTINCT product_id FROM "revenue_by_product__changelog" '
                     'WHERE _duckstring_f = (SELECT max(_duckstring_f) FROM "revenue_by_product__changelog")').fetchall()
    assert latest == [("p1",)]
    snk.close()


def test_builder_sql_requires_alias_and_select(tmp_path):
    snk = duckdb.connect()
    pond = Pond("x", "1.0.0", snk, root=tmp_path, source_majors={"a": 1, "b": 1}, f=ts(1))
    with pytest.raises(BuildError, match="alias"):
        pond.trickle("a.t").sql("SELECT 1")
    with pytest.raises(BuildError, match="select"):
        pond.trickle("a.t").join(pond.trickle("b.u"), on="k").alias("x").sql("SELECT 1 FROM x")
    snk.close()


def test_duckdb_to_ibis_type_map():
    from duckstring.trickle_builder import _duckdb_to_ibis

    assert _duckdb_to_ibis("BIGINT") == "int64"
    assert _duckdb_to_ibis("VARCHAR") == "string"
    assert _duckdb_to_ibis("DOUBLE") == "float64"
    assert _duckdb_to_ibis("TIMESTAMP WITH TIME ZONE") == "timestamp('UTC')"
    assert _duckdb_to_ibis("DECIMAL(18,2)") == "decimal(18,2)"
    with pytest.raises(BuildError, match="no Ibis mapping"):
        _duckdb_to_ibis("INTEGER[]")


def test_builder_schema_and_to_ibis_schema(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    pond = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=ts(1), previous_f=NEVER)
    priced = (
        pond.trickle("sales.order_line").alias("o")
            .join(pond.trickle("catalog.product").alias("p"), on="product_id")
            .select("CAST(o.order_id AS BIGINT) AS order_id, CAST(o.qty * p.price AS DOUBLE) AS revenue")
            .merge("priced", pk="order_id")
    )
    assert priced.schema() == {"order_id": "BIGINT", "revenue": "DOUBLE"}
    assert priced.to_ibis_schema() == {"order_id": "int64", "revenue": "float64"}
    snk.close()


def test_builder_sql_accepts_ibis_expression(tmp_path):
    ibis = pytest.importorskip("ibis")
    _cons, ol, pr = _star_sources(tmp_path)
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "rev" / "m1" / "data"
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p1", 3)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    pond = Pond("rev", "1.0.0", snk, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=ts(1), previous_f=NEVER)
    priced = (
        pond.trickle("sales.order_line").alias("o")
            .join(pond.trickle("catalog.product").alias("p"), on="product_id")
            .select("o.order_id, o.product_id, o.qty * p.price AS revenue")
            .merge("priced_line", pk="order_id")
    )
    pl = ibis.table(priced.to_ibis_schema(), name="pl")
    agg = pl.group_by("product_id").aggregate(total_revenue=pl.revenue.sum())
    priced.alias("pl").sql(agg).merge("revenue_by_product", pk="product_id")
    publish(snk, snk_dir, f=ts(1))
    assert rows(snk, snk_dir, "revenue_by_product", "product_id, total_revenue") == [("p1", 25), ("p2", 9)]
    snk.close()


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
             .select("s0.order_id, s0.product_id, s1.price").merge("priced", pk="order_id"))
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


# ─── a merge Ripple threading through the local runner ───────────────────────────


def _trickle_project(tmp_path: Path) -> Path:
    (tmp_path / "pond.toml").write_text(
        '[pond]\nname = "loud"\nversion = "0.1.0"\n[sources]\nsrc = "1.0.0"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pond.py").write_text(textwrap.dedent("""
        from duckstring import ripple

        @ripple
        def loud(pond):
            pond.read_table("src.dim")          # registers the Source as the view `dim`
            pond.merge_table("loud", pond.con.sql("SELECT id, upper(v) AS v FROM dim"), pk="id")
    """))
    (tmp_path / "src" / "puddles.py").write_text(textwrap.dedent("""
        from duckstring import puddle

        @puddle("src.dim")
        def src_dim(p):
            return p.con.sql("SELECT 1 AS id, 'a' AS v UNION ALL SELECT 2 AS id, 'b' AS v")
    """))
    return tmp_path


def test_merge_ripple_via_local_runner(tmp_path):
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
