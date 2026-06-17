"""Trickle: incremental I/O (plans/trickle.md). The append/merge write paths + change detection, the
windowed ``source.delta`` read (collapse / coverage fallback / bootstrap), the partial-path helpers,
and the data-plane publish (system columns allowed, mode/PK sidecar). Pinned to the Parquet plane (the
window read is a content predicate over ``_duckstring_f``, so it's plane-agnostic; Parquet is fast/offline)."""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane
from duckstring.engine.core import NEVER
from duckstring.local import hydrate, load_project, run_pond

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _parquet_plane(monkeypatch):
    # Pond.read_delta / read_table go through get_data_plane(); keep them on the offline Parquet plane.
    monkeypatch.setenv("DUCKSTRING_DATA_PLANE", "parquet")


@pytest.fixture
def reg(tmp_path):
    """A persistent producer registry (so Trickle history accumulates across simulated runs)."""
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    yield con
    con.close()


def publish(con, data_dir):
    ParquetDataPlane().export(con, data_dir)
    return data_dir


def rows(con, data_dir, table, proj="*"):
    sql = ParquetDataPlane().read_select(data_dir, table)
    return sorted(con.sql(f"SELECT {proj} FROM ({sql})").fetchall())


# ─── append ───────────────────────────────────────────────────────────────────


def test_append_accumulates_history_stamped_with_f(reg):
    T.append_table(reg, "event", reg.sql("SELECT 1 AS id, 'a' AS v"), ts(1), ("id",))
    T.append_table(reg, "event", reg.sql("SELECT 2 AS id, 'b' AS v"), ts(2), ("id",))
    got = reg.sql('SELECT id, v, _duckstring_f FROM event ORDER BY id').fetchall()
    assert got == [(1, "a", ts(1)), (2, "b", ts(2))]


def test_append_idempotent_replay_at_same_f(reg):
    rel = reg.sql("SELECT 1 AS id, 'a' AS v")
    T.append_table(reg, "event", rel, ts(1), ("id",))
    T.append_table(reg, "event", rel, ts(1), ("id",))  # replay/retry at the same freshness
    assert reg.sql("SELECT count(*) FROM event").fetchone()[0] == 1


def test_append_delta_window_and_meta_sidecar(reg, tmp_path):
    T.append_table(reg, "event", reg.sql("SELECT 1 AS id"), ts(1), ("id",))
    T.append_table(reg, "event", reg.sql("SELECT 2 AS id"), ts(2), ("id",))
    T.append_table(reg, "event", reg.sql("SELECT 3 AS id"), ts(3), ("id",))
    data_dir = publish(reg, tmp_path / "data")

    _sc = T.load_sidecar(data_dir)["event"]
    assert _sc["mode"] == "append" and _sc["pk"] == ["id"]
    rcon = duckdb.connect()
    d = T.read_delta(rcon, data_dir, "event", previous_f=ts(1), f=ts(3), dp=ParquetDataPlane())
    assert sorted(d.upserts.fetchall()) == [(2,), (3,)]  # (prev, f] — excludes 1, includes 3
    assert d.deletes.fetchall() == []
    # _duckstring_f is stripped from the user-facing upserts.
    assert "_duckstring_f" not in d.upserts.columns


# ─── merge: comprehensive ───────────────────────────────────────────────────────


def _state(con, triples):
    if not triples:
        return con.sql("SELECT CAST(NULL AS INTEGER) AS id, CAST(NULL AS VARCHAR) AS v WHERE 1=0")
    values = ", ".join(f"({i}, '{v}')" for i, v in triples)
    return con.sql(f"SELECT * FROM (VALUES {values}) AS s(id, v)")


def test_merge_comprehensive_diffs_insert_update_delete(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",), comprehensive=True)
    # 1 unchanged, 2 updated, 3 inserted, (none removed yet)
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "B"), (3, "c")]), ts(2), ("id",), comprehensive=True)
    # 2 stays, 1 removed, 3 removed → only 2 remains
    T.merge_table(reg, "dim", _state(reg, [(2, "B")]), ts(3), ("id",), comprehensive=True)

    data_dir = publish(reg, tmp_path / "data")
    # main is the clean current state — one row per PK, no tombstones.
    assert rows(reg, data_dir, "dim", "id, v") == [(2, "B")]
    # changelog carries the CDC ops.
    clog = reg.sql(
        'SELECT id, v, _duckstring_op, _duckstring_f FROM dim__changelog ORDER BY _duckstring_f, id'
    ).fetchall()
    assert clog == [
        # run 1 (bootstrap) writes NO changelog — re-emitting all rows is dead weight (consumers
        # bootstrap from the main); the floor marks "full-read below run 1".
        (2, "B", "upsert", ts(2)), (3, "c", "upsert", ts(2)),    # run 2: 2 changed, 3 inserted (1 unchanged)
        (1, None, "delete", ts(3)), (3, None, "delete", ts(3)),  # run 3: 1 and 3 removed (2 unchanged)
    ]
    assert T.load_sidecar(data_dir)["dim"]["floor"] == ts(1).isoformat()  # floor anchored at the first run


def test_merge_comprehensive_no_hash_in_user_read(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), comprehensive=True)
    data_dir = publish(reg, tmp_path / "data")
    rcon = duckdb.connect()
    # The clean current-state read strips the framework _duckstring_hash from the main.
    full = T._strip_system(rcon.sql(ParquetDataPlane().read_select(data_dir, "dim")))
    assert "_duckstring_hash" not in full.columns
    assert sorted(full.fetchall()) == [(1, "a")]


def test_merge_comprehensive_idempotent_replay(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",), comprehensive=True)
    state2 = [(1, "a"), (2, "B")]
    T.merge_table(reg, "dim", _state(reg, state2), ts(2), ("id",), comprehensive=True)
    before = reg.sql("SELECT count(*) FROM dim__changelog").fetchone()[0]
    T.merge_table(reg, "dim", _state(reg, state2), ts(2), ("id",), comprehensive=True)  # replay at f2
    after = reg.sql("SELECT count(*) FROM dim__changelog").fetchone()[0]
    assert before == after  # no duplicate changelog rows for f2
    assert rows(reg, publish(reg, tmp_path / "d"), "dim", "id, v") == [(1, "a"), (2, "B")]


def test_merge_delta_collapse_max_f_per_pk(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",), comprehensive=True)
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (2, "b")]), ts(2), ("id",), comprehensive=True)  # 1 upd
    T.merge_table(reg, "dim", _state(reg, [(2, "b")]), ts(3), ("id",), comprehensive=True)            # 1 del
    data_dir = publish(reg, tmp_path / "data")
    rcon = duckdb.connect()
    # Window (f1, f3] sees 1: upsert@2 then delete@3 → net delete (latest op wins).
    d = T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(3), dp=ParquetDataPlane())
    assert d.upserts.fetchall() == []
    assert d.deletes.fetchall() == [(1,)]


def test_merge_delete_then_readd_resolves_present(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), comprehensive=True)
    T.merge_table(reg, "dim", _state(reg, []), ts(2), ("id",), comprehensive=True)         # delete 1
    T.merge_table(reg, "dim", _state(reg, [(1, "z")]), ts(3), ("id",), comprehensive=True)  # re-add 1
    data_dir = publish(reg, tmp_path / "data")
    rcon = duckdb.connect()
    d = T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(3), dp=ParquetDataPlane())
    assert sorted(d.upserts.fetchall()) == [(1, "z")]  # upsert@3 beats delete@2
    assert d.deletes.fetchall() == []


def test_merge_bootstrap_and_coverage_fallback_full_read(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(2), ("id",), comprehensive=True)
    data_dir = publish(reg, tmp_path / "data")
    rcon = duckdb.connect()
    # Bootstrap (previous_f = NEVER) → full read of the clean main, no deletes.
    boot = T.read_delta(rcon, data_dir, "dim", previous_f=NEVER, f=ts(9), dp=ParquetDataPlane())
    assert sorted(boot.upserts.fetchall()) == [(1, "a"), (2, "b")]
    assert boot.deletes.fetchall() == []
    # Coverage miss (previous_f older than the oldest retained stamp) → full read too.
    miss = T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(9), dp=ParquetDataPlane())
    assert sorted(miss.upserts.fetchall()) == [(1, "a"), (2, "b")]


# ─── merge: partial (comprehensive=False) ───────────────────────────────────────


def test_merge_partial_applies_upserts_and_explicit_deletes(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b"), (3, "c")]), ts(1), ("id",), comprehensive=True)
    # Partial: change 2, delete 3, leave 1 untouched.
    T.merge_table(
        reg, "dim", reg.sql("SELECT 2 AS id, 'B' AS v"), ts(2), ("id",),
        comprehensive=False, deletes=reg.sql("SELECT 3 AS id"),
    )
    data_dir = publish(reg, tmp_path / "data")
    assert rows(reg, data_dir, "dim", "id, v") == [(1, "a"), (2, "B")]  # 1 untouched, 2 updated, 3 gone


def test_merge_partial_under_supplied_deletes_leave_stale_rows(reg, tmp_path):
    """Documented risk: under-merge silently corrupts. Asserted so it's an intentional contract."""
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",), comprehensive=True)
    # The truth dropped row 2, but the developer forgot to supply it in deletes → it stays (stale).
    T.merge_table(reg, "dim", reg.sql("SELECT 1 AS id, 'a' AS v"), ts(2), ("id",), comprehensive=False)
    data_dir = publish(reg, tmp_path / "data")
    assert rows(reg, data_dir, "dim", "id, v") == [(1, "a"), (2, "b")]  # 2 is stale — the footgun


def test_merge_comprehensive_rejects_deletes_argument(reg):
    with pytest.raises(T.DeltaError):
        T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), comprehensive=True,
                      deletes=reg.sql("SELECT 9 AS id"))


def test_merge_requires_primary_key(reg):
    with pytest.raises(T.DeltaError):
        T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), (), comprehensive=True)


# ─── partial-path helpers (Pond-level) ──────────────────────────────────────────


def _producer(tmp_path, source, major=1):
    """A persistent producer registry whose publishes land where a consumer Pond reads its sources."""
    data_dir = tmp_path / "ponds" / source / f"m{major}" / "data"
    return duckdb.connect(str(tmp_path / f"{source}.duckdb")), data_dir


def test_keysets_and_keys_joining_star_enrichment(tmp_path):
    # Source A (spine): order_line(order_id pk, product_id). Source B (dim): product(product_id pk, price).
    ol_con, ol_dir = _producer(tmp_path, "sales")
    pr_con, pr_dir = _producer(tmp_path, "catalog")
    T.merge_table(ol_con, "order_line",
                  ol_con.sql("SELECT * FROM (VALUES (10, 'p1'), (11, 'p2')) v(order_id, product_id)"),
                  ts(1), ("order_id",), comprehensive=True)
    T.merge_table(pr_con, "product",
                  pr_con.sql("SELECT * FROM (VALUES ('p1', 5), ('p2', 9)) v(product_id, price)"),
                  ts(1), ("product_id",), comprehensive=True)
    publish(ol_con, ol_dir)
    publish(pr_con, pr_dir)

    # Run 2: product p1 price changes (a dimension change that ripples to order_lines using p1).
    T.merge_table(pr_con, "product",
                  pr_con.sql("SELECT * FROM (VALUES ('p1', 50), ('p2', 9)) v(product_id, price)"),
                  ts(2), ("product_id",), comprehensive=True)
    publish(pr_con, pr_dir)

    rcon = duckdb.connect()
    pond = Pond("priced", "1.0.0", rcon, root=tmp_path,
                source_majors={"sales": 1, "catalog": 1}, f=ts(2), previous_f=ts(1))
    pr_delta = pond.read_delta("catalog.product")
    assert sorted(pr_delta.keys().relation.fetchall()) == [("p1",)]  # only p1 changed

    # Which order_line spine keys does the p1 change ripple to?
    affected = pond.keys_joining("sales.order_line", pr_delta, on="product_id")
    assert sorted(affected.relation.fetchall()) == [(10,)]  # order 10 uses p1


def test_keys_joining_rejects_non_pk_arity(tmp_path):
    ol_con, ol_dir = _producer(tmp_path, "sales")
    pr_con, pr_dir = _producer(tmp_path, "catalog")
    T.merge_table(ol_con, "order_line",
                  ol_con.sql("SELECT 10 AS order_id, 'p1' AS product_id"), ts(1), ("order_id",), comprehensive=True)
    T.merge_table(pr_con, "product",
                  pr_con.sql("SELECT 'p1' AS product_id, 1 AS region, 5 AS price"),
                  ts(1), ("product_id", "region"), comprehensive=True)  # composite PK (arity 2)
    publish(ol_con, ol_dir)
    publish(pr_con, pr_dir)
    rcon = duckdb.connect()
    pond = Pond("priced", "1.0.0", rcon, root=tmp_path,
                source_majors={"sales": 1, "catalog": 1}, f=ts(2), previous_f=ts(1))
    delta = pond.read_delta("catalog.product")
    with pytest.raises(ValueError, match="full PK"):
        pond.keys_joining("sales.order_line", delta, on="product_id")  # 1 col vs PK arity 2


def test_dropped_includes_source_deleted_keys(tmp_path):
    con = duckdb.connect()
    affected = T.KeySet(con, con.sql("SELECT * FROM (VALUES (1), (2), (3)) v(id)"), ("id",))
    recomputed = con.sql("SELECT * FROM (VALUES (1, 'x'), (2, 'y')) v(id, val)")  # 3 fell out
    assert sorted(affected.dropped(recomputed).fetchall()) == [(3,)]


# ─── end-to-end consumer merge (Trickle → Trickle) ──────────────────────────────


def test_consumer_merge_tracks_source_incrementally(tmp_path):
    src_con, src_dir = _producer(tmp_path, "src")
    T.merge_table(src_con, "dim",
                  src_con.sql("SELECT * FROM (VALUES (1, 'a'), (2, 'b')) v(id, v)"),
                  ts(1), ("id",), comprehensive=True)
    publish(src_con, src_dir)

    # Consumer run 1 (bootstrap): full read → comprehensive merge into its own state.
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "snk" / "m1" / "data"

    def consume(f, previous_f):
        pond = Pond("snk", "1.0.0", snk_con, root=tmp_path,
                    source_majors={"src": 1}, f=f, previous_f=previous_f)
        d = pond.read_delta("src.dim")
        up = d.upserts.project("id, upper(v) AS v")
        pond.merge_table("loud", up, comprehensive=False, deletes=d.deletes, pk=("id",))
        publish(snk_con, snk_dir)

    consume(ts(1), NEVER)
    assert rows(snk_con, snk_dir, "loud", "id, v") == [(1, "A"), (2, "B")]

    # Source run 2: update 1, delete 2, add 3.
    T.merge_table(src_con, "dim",
                  src_con.sql("SELECT * FROM (VALUES (1, 'z'), (3, 'c')) v(id, v)"),
                  ts(2), ("id",), comprehensive=True)
    publish(src_con, src_dir)

    consume(ts(2), ts(1))
    assert rows(snk_con, snk_dir, "loud", "id, v") == [(1, "Z"), (3, "C")]
    snk_con.close()
    src_con.close()


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
    # The @trickle(pk="id") default flowed to merge_table — a clean merge main was published, with the
    # floor anchored at this first (bootstrap) run and an empty changelog (no wasteful re-emit).
    _sc = T.load_sidecar(out)["loud"]
    assert _sc["mode"] == "merge" and _sc["pk"] == ["id"] and _sc["floor"] is not None
    con = duckdb.connect()
    assert rows(con, out, "loud", "id, v") == [(1, "A"), (2, "B")]
    assert con.sql(
        f"SELECT count(*) FROM ({ParquetDataPlane().read_select(out, 'loud__changelog')})"
    ).fetchone()[0] == 0  # bootstrap → empty changelog


# ─── retention (lag SLA; correctness never depends on it) ────────────────────────


def test_retention_retain_n_keeps_newest_runs(reg):
    for h, v in [(1, "a"), (2, "b"), (3, "c"), (4, "d")]:
        T.merge_table(reg, "dim", _state(reg, [(1, v)]), ts(h), ("id",), comprehensive=True, retain_n=2)
    kept = [r[0] for r in reg.sql("SELECT DISTINCT _duckstring_f FROM dim__changelog ORDER BY 1").fetchall()]
    assert kept == [ts(3), ts(4)]  # only the newest 2 runs survive


def test_retention_retain_t_drops_old(reg):
    from datetime import timedelta

    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), comprehensive=True, retain_t=timedelta(hours=2))
    T.merge_table(reg, "dim", _state(reg, [(1, "b")]), ts(4), ("id",), comprehensive=True, retain_t=timedelta(hours=2))
    # cutoff at run 2 = ts(4) - 2h = ts(2); the ts(1) row is older → trimmed, the current run is kept.
    kept = [r[0] for r in reg.sql("SELECT DISTINCT _duckstring_f FROM dim__changelog ORDER BY 1").fetchall()]
    assert kept == [ts(4)]


def test_append_retention_and_window(reg):
    for h in (1, 2, 3):
        T.append_table(reg, "event", reg.sql(f"SELECT {h} AS id"), ts(h), ("id",), retain_n=2)
    assert sorted(r[0] for r in reg.sql("SELECT id FROM event").fetchall()) == [2, 3]  # 1 trimmed


def test_retention_then_coverage_fallback_full_read(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",), comprehensive=True)
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (2, "b")]), ts(3), ("id",), comprehensive=True, retain_n=1)
    data_dir = publish(reg, tmp_path / "data")  # changelog now retains only ts(3)
    rcon = duckdb.connect()
    # A consumer last at ts(1) is now behind the retained window → coverage miss → full read of the main.
    d = T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(9), dp=ParquetDataPlane())
    assert sorted(d.upserts.fetchall()) == [(1, "A"), (2, "b")]
    assert d.deletes.fetchall() == []


# ─── affected_groups (aggregation sibling) ───────────────────────────────────────


def test_affected_groups_upserts_only_and_with_deletes(tmp_path):
    con = duckdb.connect()
    up = con.sql("SELECT * FROM (VALUES (1, 'x'), (2, 'y')) v(id, region)")
    de = con.sql("SELECT * FROM (VALUES (3)) v(id)")  # a delete carries only the PK
    delta = T.Delta(con, ("id",), up, de)
    pond = Pond("p", "1.0.0", con, root=tmp_path)
    # by=region is not ⊆ pk(id) → deletes can't supply their group → upserts only.
    assert sorted(pond.affected_groups(delta, by="region").relation.fetchall()) == [("x",), ("y",)]
    # by=id IS ⊆ pk → the deleted key's group is known and included.
    assert sorted(pond.affected_groups(delta, by="id").relation.fetchall()) == [(1,), (2,), (3,)]


# ─── builder DSL (pond.trickle) ──────────────────────────────────────────────────


def _star_sources(tmp_path):
    ol_con, ol_dir = _producer(tmp_path, "sales")
    pr_con, pr_dir = _producer(tmp_path, "catalog")

    def ol(state, f):
        vals = ", ".join(f"({o}, '{p}', {q})" for o, p, q in state)
        T.merge_table(ol_con, "order_line", ol_con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id, qty)"),
                      f, ("order_id",), comprehensive=True)
        publish(ol_con, ol_dir)

    def pr(state, f):
        vals = ", ".join(f"('{p}', {pr_})" for p, pr_ in state)
        T.merge_table(pr_con, "product", pr_con.sql(f"SELECT * FROM (VALUES {vals}) v(product_id, price)"),
                      f, ("product_id",), comprehensive=True)
        publish(pr_con, pr_dir)

    return (ol_con, pr_con), ol, pr


def _priced(tmp_path, f, previous_f, snk_con, snk_dir):
    pond = Pond("priced", "1.0.0", snk_con, root=tmp_path,
                source_majors={"sales": 1, "catalog": 1}, f=f, previous_f=previous_f)
    (pond.trickle("sales.order_line")
         .join(pond.trickle("catalog.product"), on="product_id")
         .select("s0.order_id, s0.product_id, s0.qty, s1.price, s0.qty * s1.price AS total")
         .merge("priced"))
    publish(snk_con, snk_dir)


def test_builder_star_enrichment_incremental(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"

    _priced(tmp_path, ts(1), NEVER, snk_con, snk_dir)  # bootstrap
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 10), (11, 9)]

    # A dimension change: p1 price 5 → 50. Only order 10 (uses p1) should be recomputed.
    pr([("p1", 50), ("p2", 9)], ts(2))
    _priced(tmp_path, ts(2), ts(1), snk_con, snk_dir)
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 100), (11, 9)]
    snk_con.close()


def test_builder_propagates_spine_delete(tmp_path):
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1)], ts(1))
    pr([("p1", 5), ("p2", 9)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"
    _priced(tmp_path, ts(1), NEVER, snk_con, snk_dir)

    ol([(10, "p1", 2)], ts(2))  # order 11 removed from the spine
    _priced(tmp_path, ts(2), ts(1), snk_con, snk_dir)
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 10)]  # 11 dropped from the output
    snk_con.close()


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

    # The incremental builder result must equal a full comprehensive recompute over the same final state.
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


def _priced_p(tmp_path, f, previous_f, snk_con, snk_dir, *, spine_p=0.3, dim_p=0.3):
    """Like :func:`_priced` but with per-source change-fraction thresholds on the builder."""
    pond = Pond("priced", "1.0.0", snk_con, root=tmp_path,
                source_majors={"sales": 1, "catalog": 1}, f=f, previous_f=previous_f)
    (pond.trickle("sales.order_line", p=spine_p)
         .join(pond.trickle("catalog.product", p=dim_p), on="product_id")
         .select("s0.order_id, s0.product_id, s0.qty, s1.price, s0.qty * s1.price AS total")
         .merge("priced"))
    publish(snk_con, snk_dir)


def _spy_merge_mode(monkeypatch):
    """Record the ``comprehensive`` flag of each Pond.merge_table call (to see which path the builder took)."""
    modes: list[bool] = []
    orig = Pond.merge_table

    def spy(self, name, relation, *, comprehensive=True, **kw):
        modes.append(comprehensive)
        return orig(self, name, relation, comprehensive=comprehensive, **kw)

    monkeypatch.setattr(Pond, "merge_table", spy)
    return modes


def test_builder_threshold_falls_back_to_comprehensive(tmp_path, monkeypatch):
    """When a source's delta exceeds its change-fraction threshold ``p``, ``.merge()`` abandons the
    partial slice and recomputes comprehensively — and the output is still correct."""
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p3", 3)], ts(1))
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"
    _priced_p(tmp_path, ts(1), NEVER, snk_con, snk_dir)  # bootstrap (full → comprehensive)

    modes = _spy_merge_mode(monkeypatch)
    # All 3 of 3 product prices change → 100% of the dimension's keys > p=0.3 → comprehensive fallback.
    pr([("p1", 50), ("p2", 90), ("p3", 70)], ts(2))
    _priced_p(tmp_path, ts(2), ts(1), snk_con, snk_dir)
    assert modes == [True], "over-threshold run should take the comprehensive path"
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 100), (11, 90), (12, 210)]
    snk_con.close()


def test_builder_threshold_p1_forces_incremental(tmp_path, monkeypatch):
    """``p=1.0`` disables the check for that source — the builder stays on the incremental path even when
    every key changed (and never pays for the count)."""
    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2), (11, "p2", 1), (12, "p3", 3)], ts(1))
    pr([("p1", 5), ("p2", 9), ("p3", 7)], ts(1))
    snk_con = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"
    _priced_p(tmp_path, ts(1), NEVER, snk_con, snk_dir, dim_p=1.0)

    modes = _spy_merge_mode(monkeypatch)
    pr([("p1", 50), ("p2", 90), ("p3", 70)], ts(2))  # 100% churn, but dim_p=1.0 → stays incremental
    _priced_p(tmp_path, ts(2), ts(1), snk_con, snk_dir, dim_p=1.0)
    assert modes == [False], "p=1.0 should keep the incremental (comprehensive=False) path"
    assert rows(snk_con, snk_dir, "priced", "order_id, total") == [(10, 100), (11, 90), (12, 210)]
    snk_con.close()


def test_builder_build_time_errors(tmp_path):
    from duckstring.trickle_builder import BuildError

    _cons, ol, pr = _star_sources(tmp_path)
    ol([(10, "p1", 2)], ts(1))
    pr([("p1", 5)], ts(1))
    con = duckdb.connect()
    pond = Pond("priced", "1.0.0", con, root=tmp_path, source_majors={"sales": 1, "catalog": 1}, f=ts(1))

    # A snowflake (a dimension that itself has joins) is outside the op set.
    snowflake_dim = pond.trickle("catalog.product").join(pond.trickle("catalog.product"), on="product_id")
    with pytest.raises(BuildError, match="snowflake|deeper hop"):
        pond.trickle("sales.order_line").join(snowflake_dim, on="product_id")

    # A non-PK-arity join key.
    with pytest.raises(BuildError, match="full PK|arity|column"):
        pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on=("product_id", "qty"))

    # A joined graph with no .select(...).
    with pytest.raises(BuildError, match="select"):
        pond.trickle("sales.order_line").join(pond.trickle("catalog.product"), on="product_id").merge("x")


# ─── a Trickle following a Ripple (overwrite source) ─────────────────────────────


def test_trickle_follows_overwrite_ripple_via_comprehensive(tmp_path):
    """A Trickle can sit downstream of a plain (overwrite) Ripple: it full-reads at that hop and merges
    comprehensively. The incremental win only applies to Trickle→Trickle sequences after it — but the
    output is correct, including deletes the comprehensive diff infers from the overwrite snapshot."""
    rip_dir = tmp_path / "ponds" / "rip" / "m1" / "data"
    rip_dir.mkdir(parents=True)
    rip = duckdb.connect()
    rip.execute("CREATE TABLE dim AS SELECT * FROM (VALUES (1, 'a'), (2, 'b')) t(id, v)")
    ParquetDataPlane().export(rip, rip_dir)  # overwrite publish — no sidecar, no pk

    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))

    def consume(f, previous_f):
        pond = Pond("snk", "1.0.0", snk, root=tmp_path, source_majors={"rip": 1}, f=f, previous_f=previous_f)
        pond.read_table("rip.dim")  # full read of the overwrite source (system-col-free)
        pond.merge_table("loud", snk.sql("SELECT id, upper(v) AS v FROM dim"), pk=("id",))

    consume(ts(1), NEVER)
    assert sorted(snk.sql("SELECT id, v FROM loud").fetchall()) == [(1, "A"), (2, "B")]

    rip.execute("DROP TABLE dim; CREATE TABLE dim AS SELECT * FROM (VALUES (1, 'a')) t(id, v)")
    ParquetDataPlane().export(rip, rip_dir)  # the Ripple dropped row 2
    consume(ts(2), ts(1))
    assert sorted(snk.sql("SELECT id, v FROM loud").fetchall()) == [(1, "A")]  # delete inferred
    clog = snk.sql("SELECT id, _duckstring_op FROM loud__changelog ORDER BY _duckstring_f, id").fetchall()
    assert (2, "delete") in clog


def test_builder_over_overwrite_source_raises_with_guidance(tmp_path):
    from duckstring.trickle_builder import BuildError

    rip_dir = tmp_path / "ponds" / "rip" / "m1" / "data"
    rip_dir.mkdir(parents=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE dim AS SELECT 1 AS id")
    ParquetDataPlane().export(con, rip_dir)
    pond = Pond("snk", "1.0.0", con, root=tmp_path, source_majors={"rip": 1}, f=ts(1))
    with pytest.raises(BuildError, match="not a Trickle"):
        pond.trickle("rip.dim")


def test_comprehensive_replay_preserves_changelog_after_main_advanced(reg, tmp_path):
    """Crash-safety: the changelog window is rewritten BEFORE the main is overwritten, and only when the
    derived delta is non-empty. So a replay after the main already advanced (the diff now yields nothing)
    leaves the changelog rows the first attempt wrote intact — they are never lost."""
    T.merge_table(reg, "dim", _state(reg, [(1, "a"), (2, "b")]), ts(1), ("id",), comprehensive=True)
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (2, "b")]), ts(2), ("id",), comprehensive=True)
    clog_at_f2 = reg.execute("SELECT id, _duckstring_op FROM dim__changelog WHERE _duckstring_f = ?", [ts(2)]).fetchall()
    assert clog_at_f2 == [(1, "upsert")]  # run 2 changed id 1

    # Simulate the post-crash replay state: the main already reflects f2 (committed before the crash),
    # and the run re-executes at the same f2 with the same output.
    T.merge_table(reg, "dim", _state(reg, [(1, "A"), (2, "b")]), ts(2), ("id",), comprehensive=True)
    replayed = reg.execute("SELECT id, _duckstring_op FROM dim__changelog WHERE _duckstring_f = ?",
                           [ts(2)]).fetchall()
    assert replayed == [(1, "upsert")]  # not lost, not duplicated


# ─── incremental draw (cross-Catchment transfer window) ──────────────────────────


def test_incremental_draw_window_roundtrip(tmp_path):
    """A consumer behind by some runs fetches only the changelog rows newer than what it has landed and
    merges them in — the producer ships a small delta, the consumer ends row-for-row identical to it."""
    import shutil

    prod, cons = tmp_path / "prod", tmp_path / "cons"
    con = duckdb.connect()

    def producer_run(rows, hour):
        vals = ", ".join(f"({i}, '{v}')" for i, v in rows)
        T.merge_table(con, "dim", con.sql(f"SELECT * FROM (VALUES {vals}) t(id, v)"),
                      ts(hour), ("id",), comprehensive=True)
        ParquetDataPlane().export(con, prod)

    producer_run([(1, "a"), (2, "b")], 1)        # bootstrap — empty changelog, floor = run 1
    producer_run([(1, "A"), (3, "c")], 2)        # run 2 changelog: 1 upd, 2 del, 3 add
    # Consumer bootstraps wholesale after run 2 (copies the producer's state — changelog has run-2 rows).
    cons.mkdir()
    for f in prod.iterdir():
        shutil.copy(f, cons / f.name)
    assert T.landed_after(cons) == ts(2).isoformat()  # holds everything up to run 2 (floor=1, rows up to 2)

    producer_run([(1, "A"), (3, "C"), (4, "d")], 3)  # run 3: 3 changed, 4 added (producer now ahead)

    # Incremental transfer: only the changelog rows > after (run 3); the main is wholesale.
    after = T.landed_after(cons)
    shipped = T.window_parquet_bytes(prod / "dim__changelog.parquet", after)
    (tmp_path / "shipped.parquet").write_bytes(shipped)
    rcon = duckdb.connect()
    rcon.execute("SET TimeZone='UTC'")
    assert rcon.sql(
        f"SELECT DISTINCT _duckstring_f FROM read_parquet('{tmp_path / 'shipped.parquet'}')"
    ).fetchall() == [(ts(3),)]  # the slice carries ONLY run 3, not earlier runs

    # land the slice (merge, not replace) + the wholesale main, then read incrementally
    T.land_windowed(cons / "dim__changelog.parquet", shipped, after)
    shutil.copy(prod / "dim.parquet", cons / "dim.parquet")

    d = T.read_delta(rcon, cons, "dim", previous_f=ts(2), f=ts(3), dp=ParquetDataPlane())
    assert sorted(d.upserts.fetchall()) == [(3, "C"), (4, "d")]  # run 3's net change
    assert d.deletes.fetchall() == []
    # The landed changelog spans runs 2 and 3 — the slice was merged in, not replaced.
    assert rcon.sql(
        f"SELECT count(DISTINCT _duckstring_f) FROM read_parquet('{cons / 'dim__changelog.parquet'}')"
    ).fetchone()[0] == 2


def test_landed_after_bootstrap_is_none(tmp_path):
    # No sidecar (nothing landed yet) → wholesale transfer.
    assert T.landed_after(tmp_path) is None


# ─── D1: full-read absorption (the coverage-miss / retention-lag delete bug) ──────


def test_delta_is_full_flag_per_branch(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), comprehensive=True)
    T.merge_table(reg, "dim", _state(reg, [(1, "b")]), ts(2), ("id",), comprehensive=True)
    data_dir = publish(reg, tmp_path / "data")
    rcon = duckdb.connect()
    dp = ParquetDataPlane()
    assert T.read_delta(rcon, data_dir, "dim", previous_f=NEVER, f=ts(9), dp=dp).is_full      # bootstrap
    assert T.read_delta(rcon, data_dir, "dim", previous_f=ts(0), f=ts(9), dp=dp).is_full       # coverage miss
    assert not T.read_delta(rcon, data_dir, "dim", previous_f=ts(1), f=ts(2), dp=dp).is_full    # windowed


def test_builder_absorbs_coverage_miss_comprehensively(tmp_path):
    """The bug: a partial/builder consumer that falls behind a source's retained changelog gets a
    full-read fallback (empty deletes) and would keep stale rows. With is_full → comprehensive, the
    builder drops the vanished row instead."""
    ol_con, ol_dir = _producer(tmp_path, "sales")
    pr_con, pr_dir = _producer(tmp_path, "catalog")

    def order_lines(state, f):
        vals = ", ".join(f"({o}, '{p}')" for o, p in state)
        T.merge_table(ol_con, "order_line", ol_con.sql(f"SELECT * FROM (VALUES {vals}) v(order_id, product_id)"),
                      f, ("order_id",), comprehensive=True, retain_n=1)  # retain only the newest run
        publish(ol_con, ol_dir)

    T.merge_table(pr_con, "product", pr_con.sql("SELECT * FROM (VALUES ('p1', 5), ('p2', 9)) v(product_id, price)"),
                  ts(1), ("product_id",), comprehensive=True)
    publish(pr_con, pr_dir)
    order_lines([(10, "p1"), (11, "p2")], ts(1))

    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    snk_dir = tmp_path / "ponds" / "priced" / "m1" / "data"

    def build(f, previous_f):
        pond = Pond("priced", "1.0.0", snk, root=tmp_path,
                    source_majors={"sales": 1, "catalog": 1}, f=f, previous_f=previous_f)
        (pond.trickle("sales.order_line")
             .join(pond.trickle("catalog.product"), on="product_id")
             .select("s0.order_id, s0.product_id, s1.price").merge("priced"))
        publish(snk, snk_dir)

    build(ts(1), NEVER)
    assert rows(snk, snk_dir, "priced", "order_id") == [(10,), (11,)]

    # Two more source runs with retain_n=1 trim the changelog past the consumer's ts(1) watermark, and
    # order 11 is dropped. The consumer coverage-misses → comprehensive absorb → 11 must disappear.
    order_lines([(10, "p1")], ts(2))   # 11 removed
    order_lines([(10, "p1")], ts(3))   # advance again so the changelog floor passes ts(1)
    build(ts(3), ts(1))
    assert rows(snk, snk_dir, "priced", "order_id") == [(10,)]  # 11 dropped, not stale
    snk.close()


# ─── coverage floor (bootstrap anchor + retention + refresh signal) ──────────────


def test_floor_anchors_at_bootstrap_and_retention_advances_it(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), comprehensive=True)         # bootstrap
    assert T.load_sidecar(publish(reg, tmp_path / "d1"))["dim"]["floor"] == ts(1).isoformat()
    T.merge_table(reg, "dim", _state(reg, [(1, "b")]), ts(2), ("id",), comprehensive=True)         # floor stays 1
    assert T.load_sidecar(publish(reg, tmp_path / "d2"))["dim"]["floor"] == ts(1).isoformat()
    # Retention trims run-2 down to the newest 1 → floor rises to the surviving min.
    T.merge_table(reg, "dim", _state(reg, [(1, "c")]), ts(3), ("id",), comprehensive=True, retain_n=1)
    assert T.load_sidecar(publish(reg, tmp_path / "d3"))["dim"]["floor"] == ts(3).isoformat()


def test_floor_forces_full_read_for_a_lagging_consumer(reg, tmp_path):
    T.merge_table(reg, "dim", _state(reg, [(1, "a")]), ts(1), ("id",), comprehensive=True)
    T.merge_table(reg, "dim", _state(reg, [(1, "b")]), ts(3), ("id",), comprehensive=True, retain_n=1)  # floor→3
    data_dir = publish(reg, tmp_path / "data")
    rcon = duckdb.connect()
    dp = ParquetDataPlane()
    # A consumer last at ts(2) is behind the floor (3) — even though a changelog row exists, it must
    # full-read (is_full) the clean main, not trust a partial window.
    d = T.read_delta(rcon, data_dir, "dim", previous_f=ts(2), f=ts(9), dp=dp)
    assert d.is_full and sorted(d.upserts.fetchall()) == [(1, "b")]
    # A consumer at the floor reads the window incrementally.
    assert not T.read_delta(rcon, data_dir, "dim", previous_f=ts(3), f=ts(9), dp=dp).is_full
