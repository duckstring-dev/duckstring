"""Z-set I/O: the append and merge write paths, the merge changelog and its current-state view, windowed
delta reads (consolidation, coverage and the bootstrap full read), retention, and the write return values.
All against one database; deltas are read through the standalone host."""

from __future__ import annotations

from datetime import timedelta

import pytest
from trickle.io import first_row
from trickle_support import NEVER, T, at, merge, rows, ts


def _dim(con, state, f, **kw):
    return merge(con, "dim", state, "id, v", f, "id", types="INTEGER, VARCHAR", **kw)


# ─── append ───────────────────────────────────────────────────────────────────


def test_append_accumulates_history_stamped_with_f(db):
    T.append_table(db, "event", db.sql("SELECT 1 AS id, 'a' AS v"), ts(1), ("id",))
    T.append_table(db, "event", db.sql("SELECT 2 AS id, 'b' AS v"), ts(2), ("id",))
    assert sorted(db.sql(f"SELECT id, v, {T.F_COL} FROM event").fetchall()) == [(1, "a", ts(1)), (2, "b", ts(2))]


def test_an_all_null_column_takes_values_on_a_later_write(db):
    # DuckDB 2.0 types a bare NULL as the NULL type; stored, it would refuse the next run's values.
    T.append_table(db, "event", db.sql("SELECT 1 AS id, NULL AS v"), ts(1), ("id",))
    T.append_table(db, "event", db.sql("SELECT 2 AS id, 7 AS v"), ts(2), ("id",))
    assert sorted(db.sql("SELECT id, v FROM event").fetchall()) == [(1, None), (2, 7)]
    merge(db, "dim", [(1, None)], "id, v", ts(1), "id")
    merge(db, "dim", [(1, 5)], "id, v", ts(2), "id")
    assert rows(db, "dim") == [(1, 5)]


def test_append_idempotent_replay_at_same_f(db):
    rel = db.sql("SELECT 1 AS id, 'a' AS v")
    T.append_table(db, "event", rel, ts(1), ("id",))
    T.append_table(db, "event", rel, ts(1), ("id",))  # replay at the same epoch
    assert first_row(db.sql("SELECT count(*) FROM event"))[0] == 1


def test_append_fail_on_conflict_catches_duplicates(db):
    # fail_on_conflict defaults True; with a pk it asserts uniqueness. Duplicate within the appended batch:
    with pytest.raises(T.DeltaError, match="duplicate"):
        T.append_table(db, "event", db.sql("SELECT 1 AS id UNION ALL SELECT 1 AS id"), ts(1), ("id",))
    assert not db.sql("SELECT 1 FROM information_schema.tables WHERE table_name = 'event'").fetchall()

    # Collision against existing history (a different run's row).
    T.append_table(db, "event", db.sql("SELECT 1 AS id"), ts(1), ("id",))
    with pytest.raises(T.DeltaError, match="already present"):
        T.append_table(db, "event", db.sql("SELECT 1 AS id"), ts(2), ("id",))

    # A replay at the same f re-appends its own rows without tripping the check.
    T.append_table(db, "event", db.sql("SELECT 1 AS id"), ts(1), ("id",))
    assert first_row(db.sql("SELECT count(*) FROM event"))[0] == 1

    # fail_on_conflict=False is the trust-the-writer fast path: a colliding pk is appended without a check.
    T.append_table(db, "event", db.sql("SELECT 1 AS id"), ts(3), ("id",), fail_on_conflict=False)
    assert first_row(db.sql("SELECT count(*) FROM event"))[0] == 2


def test_append_delta_window_is_plus_one(db):
    for h in (1, 2, 3):
        T.append_table(db, "event", db.sql(f"SELECT {h} AS id"), ts(h), ("id",))
    d = at(db, ts(3), ts(1)).read_delta("event")
    assert sorted(d.zset.fetchall()) == [(2, 1), (3, 1)]  # rows 2 and 3 at weight +1 (append never retracts)
    assert not d.is_full


# ─── merge: the Z-set changelog ─────────────────────────────────────────────────


def test_merge_changelog_is_full_image_zset(db):
    _dim(db, [(1, "a"), (2, "b")], ts(1))            # bootstrap
    _dim(db, [(1, "a"), (2, "B"), (3, "c")], ts(2))  # 2 updated, 3 inserted
    _dim(db, [(2, "B")], ts(3))                       # 1 and 3 deleted

    assert rows(db, "dim", "id, v") == [(2, "B")]    # the current state, no system columns
    clog = db.sql(
        f"SELECT id, v, {T.D_COL}, {T.F_COL} FROM dim__changelog ORDER BY {T.F_COL}, id, {T.D_COL}"
    ).fetchall()
    assert clog == [
        (2, "b", -1, ts(2)), (2, "B", 1, ts(2)), (3, "c", 1, ts(2)),  # run 2: update 2 = -old +new; insert 3
        (1, "a", -1, ts(3)), (3, "c", -1, ts(3)),                      # run 3: delete 1 and 3 (retractions)
    ]
    # The bootstrap is the base, not a changelog window: no consumer reads it as a delta (the floor is at it).
    base = db.sql(f"SELECT id, v, {T.F_COL} FROM dim__base ORDER BY id").fetchall()
    assert base == [(1, "a", ts(1)), (2, "b", ts(1))]
    meta = T.read_meta(db)["dim"]
    assert meta["floor"] == meta["f_base"] == ts(1).isoformat()


def test_merge_idempotent_replay(db):
    _dim(db, [(1, "a"), (2, "b")], ts(1))
    _dim(db, [(1, "a"), (2, "B")], ts(2))
    before = first_row(db.sql("SELECT count(*) FROM dim__changelog"))[0]
    _dim(db, [(1, "a"), (2, "B")], ts(2))  # replay: diffed against the state before ts(2), so identical
    assert first_row(db.sql("SELECT count(*) FROM dim__changelog"))[0] == before
    assert rows(db, "dim", "id, v") == [(1, "a"), (2, "B")]


def test_merge_window_consolidates_multiple_updates(db):
    _dim(db, [(1, "a")], ts(1))
    _dim(db, [(1, "b")], ts(2))  # a→b
    _dim(db, [(1, "c")], ts(3))  # b→c
    # Window (ts1, ts3]: -a +b -b +c consolidates to -a +c (the intermediate b cancels).
    d = at(db, ts(3), ts(1)).read_delta("dim")
    assert sorted(d.zset.fetchall()) == [(1, "a", -1), (1, "c", 1)]
    assert sorted(d.upserts.fetchall()) == [(1, "c")]
    assert d.deletes.fetchall() == []


def test_merge_delete_then_readd_resolves_present(db):
    _dim(db, [(1, "a")], ts(1))
    _dim(db, [], ts(2))          # delete 1
    _dim(db, [(1, "z")], ts(3))  # re-add 1
    d = at(db, ts(3), ts(1)).read_delta("dim")
    assert sorted(d.upserts.fetchall()) == [(1, "z")]  # net: was 'a', now 'z'
    assert d.deletes.fetchall() == []


def test_merge_bootstrap_and_coverage_fallback_full_read(db):
    _dim(db, [(1, "a"), (2, "b")], ts(2))  # the table's history starts at ts(2)
    boot = at(db, ts(9), NEVER).read_delta("dim")
    assert boot.is_full and sorted(boot.upserts.fetchall()) == [(1, "a"), (2, "b")]
    miss = at(db, ts(9), ts(1)).read_delta("dim")  # previous_f before the history starts: not covered
    assert miss.is_full and sorted(miss.upserts.fetchall()) == [(1, "a"), (2, "b")]


def test_merge_requires_primary_key(db):
    with pytest.raises(T.DeltaError):
        T.merge_table(db, "dim", db.sql("SELECT 1 AS id, 'a' AS v"), ts(1), ())


class _CrashOn:
    """Proxy a DuckDB connection, raising when an executed statement contains ``needle``, to inject a crash
    mid-apply and prove the changelog write commits or aborts as a whole."""

    def __init__(self, con, needle):
        self._c, self._needle = con, needle

    def execute(self, sql, *a, **k):
        if self._needle in sql:
            raise RuntimeError("injected crash")
        return self._c.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_merge_apply_changelog_write_is_atomic(db):
    """The main is log-structured (changelog only), so a crash mid-apply can only affect the changelog. The
    DELETE+INSERT of this f's window is one transaction: a crash rolls it back, the reconstructed state is
    unchanged, and a clean replay recovers."""
    _dim(db, [(1, "a"), (2, "b")], ts(1))
    _dim(db, [(1, "A"), (2, "b")], ts(2))

    with pytest.raises(RuntimeError):  # crash on the INSERT; the DELETE before it must roll back too
        T.merge_table(_CrashOn(db, 'INSERT INTO "dim__changelog"'),
                      "dim", db.sql("SELECT * FROM (VALUES (1, 'Z'), (2, 'b')) v(id, v)"), ts(3), ("id",))

    assert first_row(db.execute(f"SELECT count(*) FROM dim__changelog WHERE {T.F_COL} = {T._ts(ts(3))}"))[0] == 0
    assert sorted(T.reconstruct_current(db, "dim").fetchall()) == [(1, "A", ts(2)), (2, "b", ts(1))]

    _dim(db, [(1, "Z"), (2, "b")], ts(3))  # a clean replay recovers fully
    assert rows(db, "dim") == [(1, "Z"), (2, "b")]
    assert sorted(db.execute(
        f"SELECT id, v, {T.D_COL} FROM dim__changelog WHERE {T.F_COL} = {T._ts(ts(3))}"
    ).fetchall()) == [(1, "A", -1), (1, "Z", 1)]


# ─── the merge main's current-state view ─────────────────────────────────────────


def test_plain_sql_reads_current_state_before_compaction(db):
    _dim(db, [(1, "a"), (2, "b")], ts(1))
    assert sorted(db.sql("SELECT * FROM dim").fetchall()) == [(1, "a"), (2, "b")]
    assert db.sql("SELECT * FROM dim").columns == ["id", "v"]  # no system columns
    _dim(db, [(1, "A"), (3, "c")], ts(2))
    assert sorted(db.sql("SELECT * FROM dim").fetchall()) == rows(db, "dim") == [(1, "A"), (3, "c")]


def test_plain_sql_reads_current_state_after_compaction(db):
    _dim(db, [(1, "a"), (2, "b")], ts(1))
    T.checkpoint(db, "dim", ts(1))
    _dim(db, [(1, "A"), (2, "b")], ts(2))
    assert sorted(db.sql("SELECT * FROM dim").fetchall()) == rows(db, "dim") == [(1, "A"), (2, "b")]
    # The cold base is reachable by its own name, still at the compacted state with its freshness stamp.
    assert sorted(db.sql("SELECT id, v FROM dim__base").fetchall()) == [(1, "a"), (2, "b")]
    assert T.F_COL in db.sql("SELECT * FROM dim__base").columns


def test_plain_sql_includes_the_warm_tier(db):
    _dim(db, [(1, "a")], ts(1))
    _dim(db, [(1, "a"), (2, "b")], ts(2))
    _dim(db, [(1, "a"), (2, "b"), (3, "c")], ts(3))
    T.fold_warm(db, "dim", ts(2))  # moves the second run's changes into a warm band, staged until published
    assert T._table_exists(db, T.warm_pending_name("dim"))
    assert sorted(db.sql("SELECT * FROM dim").fetchall()) == rows(db, "dim") == [(1, "a"), (2, "b"), (3, "c")]


@pytest.mark.parametrize("legacy_form", ["table", "view"])
def test_legacy_base_under_the_main_name_is_migrated(db, legacy_form):
    """A registry from before this layout kept the cold base under the main's own name (a table, or a view
    over published chunks after a rebuild). The first touch moves it to ``__base``."""
    _dim(db, [(1, "a"), (2, "b")], ts(1))
    T.checkpoint(db, "dim", ts(1))
    db.execute("DROP VIEW dim")  # recreate the legacy layout: the base under "dim", no view
    if legacy_form == "table":
        db.execute("ALTER TABLE dim__base RENAME TO dim")
    else:
        db.execute("CREATE TABLE legacy_store AS SELECT * FROM dim__base")
        db.execute("DROP TABLE dim__base")
        db.execute("CREATE VIEW dim AS SELECT * FROM legacy_store")
    _dim(db, [(1, "a"), (2, "B")], ts(2))
    assert T._table_exists(db, "dim__base")
    assert T._is_current_view(db, "dim")
    assert sorted(db.sql("SELECT * FROM dim").fetchall()) == [(1, "a"), (2, "B")]


def test_drop_table_removes_the_whole_collection(db):
    _dim(db, [(1, "a")], ts(1))
    T.checkpoint(db, "dim", ts(1))
    assert "dim" in T.read_meta(db)
    T.drop_table(db, "dim")
    assert "dim" not in T.read_meta(db)
    for name in ("dim", "dim__base", "dim__changelog"):
        assert not T._table_exists(db, name)
    T.drop_table(db, "dim")  # idempotent


def test_base_table_name_resolves_companions():
    assert T.base_table_name("priced_line") == "priced_line"
    assert T.base_table_name("priced_line__changelog") == "priced_line"
    assert T.base_table_name("priced_line__band") == "priced_line"
    assert T.base_table_name("order_line__droplog") == "order_line"
    assert T.base_table_name("priced_line__base") == "priced_line"
    assert T.base_table_name("__changelog") == "__changelog"  # no base → unchanged


# ─── retention (a lag bound; correctness never depends on it) ────────────────────


def test_retention_retain_n_keeps_newest_runs(db):
    for h, v in [(1, "a"), (2, "b"), (3, "c"), (4, "d")]:
        _dim(db, [(1, v)], ts(h), retain_n=2)
    kept = [r[0] for r in db.sql(f"SELECT DISTINCT {T.F_COL} FROM dim__changelog ORDER BY 1").fetchall()]
    assert kept == [ts(3), ts(4)]


def test_retention_retain_t_drops_old(db):
    _dim(db, [(1, "a")], ts(1), retain_t=timedelta(hours=2))
    _dim(db, [(1, "b")], ts(4), retain_t=timedelta(hours=2))
    kept = [r[0] for r in db.sql(f"SELECT DISTINCT {T.F_COL} FROM dim__changelog ORDER BY 1").fetchall()]
    assert kept == [ts(4)]


def test_append_retention_and_window(db):
    for h in (1, 2, 3):
        T.append_table(db, "event", db.sql(f"SELECT {h} AS id"), ts(h), ("id",), retain_n=2)
    assert sorted(r[0] for r in db.sql("SELECT id FROM event").fetchall()) == [2, 3]


def test_floor_anchors_at_bootstrap_and_retention_advances_it(db):
    _dim(db, [(1, "a")], ts(1))
    assert T.read_meta(db)["dim"]["floor"] == ts(1).isoformat()
    _dim(db, [(1, "b")], ts(2))
    assert T.read_meta(db)["dim"]["floor"] == ts(1).isoformat()
    _dim(db, [(1, "c")], ts(3), retain_n=1)
    # Retention folds the changes before the newest run into the warm tier, so the floor is the last of them:
    # a consumer that last read at ts(2) still has its whole window in the change log.
    assert T.read_meta(db)["dim"]["floor"] == ts(2).isoformat()
    assert not at(db, ts(3), ts(2)).read_delta("dim").is_full
    assert at(db, ts(3), ts(1)).read_delta("dim").is_full  # one behind the floor reads in full


# ─── write return values (the no-change signal) ──────────────────────────────────


def test_write_return_values_signal_change(db):
    # append_table: new rows → True; an empty relation → False; a same-f replay → False.
    assert T.append_table(db, "ev", db.sql("SELECT 1 AS id"), ts(1), ("id",)) is True
    assert T.append_table(db, "ev", db.sql("SELECT CAST(NULL AS INTEGER) AS id WHERE 1=0"), ts(2), ("id",)) is False
    assert T.append_table(db, "ev", db.sql("SELECT 1 AS id"), ts(1), ("id",)) is False

    # merge_table: a real change → True; re-merging the identical state → False.
    assert _dim(db, [(1, "a"), (2, "b")], ts(1)) is True
    assert _dim(db, [(1, "a"), (2, "b")], ts(2)) is False
    assert _dim(db, [(1, "A"), (2, "b")], ts(3)) is True


@pytest.mark.parametrize("retain", [{"retain_n": 1}, {"retain_n": 2}, {"retain_t": timedelta(minutes=90)}])
def test_merge_retention_keeps_the_state(db, retain):
    """Retention bounds a merge table's change log, never its state: the dropped changes are folded into the
    warm tier first. A row written once and never changed again outlives any retention window."""
    import random

    rng = random.Random(3)
    state: dict[int, str] = {}
    for h in range(1, 13):
        for _ in range(3):
            k = rng.randint(1, 8)
            if rng.random() < 0.25:
                state.pop(k, None)
            else:
                state[k] = f"v{h}_{rng.randint(0, 9)}"
        _dim(db, sorted(state.items()), ts(h), **retain)
        assert rows(db, "dim") == sorted(state.items()), f"run {h}"
        assert sorted(db.sql("SELECT * FROM dim").fetchall()) == sorted(state.items())
    hot = [r[0] for r in db.sql(f"SELECT DISTINCT {T.F_COL} FROM dim__changelog").fetchall()]
    assert len(hot) <= retain.get("retain_n", 2)  # the change log itself is bounded
    assert at(db, ts(12), ts(1)).read_delta("dim").is_full  # a consumer behind the window reads in full
