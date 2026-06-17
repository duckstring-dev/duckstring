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

    assert T.load_sidecar(data_dir)["event"] == {"mode": "append", "pk": ["id"]}
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
        (1, "a", "upsert", ts(1)), (2, "b", "upsert", ts(1)),    # run 1: both inserted
        (2, "B", "upsert", ts(2)), (3, "c", "upsert", ts(2)),    # run 2: 2 changed, 3 inserted (1 unchanged)
        (1, None, "delete", ts(3)), (3, None, "delete", ts(3)),  # run 3: 1 and 3 removed (2 unchanged)
    ]


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
    # The @trickle(pk="id") default flowed to merge_table — the clean main + its changelog were published.
    assert T.load_sidecar(out)["loud"] == {"mode": "merge", "pk": ["id"]}
    con = duckdb.connect()
    assert rows(con, out, "loud", "id, v") == [(1, "A"), (2, "B")]
    clog = con.sql(
        f"SELECT id, v, _duckstring_op FROM ({ParquetDataPlane().read_select(out, 'loud__changelog')}) ORDER BY id"
    ).fetchall()
    assert clog == [(1, "A", "upsert"), (2, "B", "upsert")]
