"""The join delta checked against a recompute: random inserts, updates and deletes on every source, over several
runs, through every join type in binary, chained and bushy shapes. Keys repeat and can be NULL, so matches are
many-to-many and a key's first match and last match both come and go (``plans/trickle-join-delta.md``)."""

from __future__ import annotations

import random

import pytest
from trickle_support import NEVER, T, at, merge, rows, spy_paths, ts

HOWS = ["inner", "left", "right", "full", "semi", "anti"]
KEYS = [0, 1, 2, 3, None]


def _ordered(rows_) -> list:
    """Rows sorted with NULLs first (Python can't compare None with an int)."""
    return sorted(rows_, key=lambda r: tuple((v is not None, v if v is not None else 0) for v in r))


def _cols(t: str) -> list[str]:
    return [f"{t}_id", f"{t}_k", f"{t}_j", f"{t}_v"]


def _step(rng: random.Random, state: dict, t: str) -> None:
    """Change ``state`` (id → row) at random: inserts, key and value updates, deletes. Sometimes nothing."""
    if rng.random() < 0.25:
        return
    for _ in range(rng.randint(1, 4)):
        roll = rng.random()
        if roll < 0.4 or not state:
            i = max(state, default=0) + 1
            state[i] = (i, rng.choice(KEYS), rng.choice(KEYS), rng.randint(0, 9))
        elif roll < 0.75:
            i = rng.choice(sorted(state))
            _, k, j, v = state[i]
            state[i] = (i, rng.choice(KEYS), j, v) if rng.random() < 0.5 else (i, k, rng.choice(KEYS), rng.randint(0, 9))
        else:
            del state[rng.choice(sorted(state))]


# A shape is a table name, or (how, left, right, (left_col, right_col)): the left column must come from a leaf on
# the left, the right column from a leaf on the right.
def _shapes(rng: random.Random):
    a, b, c, d = "a", "b", "c", "d"
    h = [rng.choice(HOWS) for _ in range(3)]
    left_only = {"semi", "anti"}
    yield (h[0], a, b, ("a_k", "b_k"))
    # A chain: the second key comes from a or, when b's columns reach the output, from b.
    inner = (h[0], a, b, ("a_k", "b_k"))
    col = "b_j" if h[0] not in left_only and rng.random() < 0.5 else "a_j"
    yield (h[1], inner, c, (col, "c_j"))
    # Bushy: (a ⋈ b) ⋈ (c ⋈ d), the top key from either subtree.
    rcol = "d_j" if h[2] not in left_only and rng.random() < 0.5 else "c_j"
    yield (h[1], (h[0], a, b, ("a_k", "b_k")), (h[2], c, d, ("c_k", "d_k")), ("a_j", rcol))


def _tables(shape) -> list[str]:
    return [shape] if isinstance(shape, str) else _tables(shape[1]) + _tables(shape[2])


def _out_tables(shape) -> list[str]:
    if isinstance(shape, str):
        return [shape]
    how, left, right, _ = shape
    return _out_tables(left) + ([] if how in ("semi", "anti") else _out_tables(right))


def _builder(t, shape):
    if isinstance(shape, str):
        return t.source(shape, p=1.0).alias(shape)  # p=1.0: always the incremental path, never the fallback
    how, left, right, (lc, rc) = shape
    return _builder(t, left).join(_builder(t, right), on={lc: rc}, how=how)


def _sql(shape) -> str:
    if isinstance(shape, str):
        return f"(SELECT * FROM {shape})"
    how, left, right, (lc, rc) = shape
    join = {"inner": "JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL JOIN",
            "semi": "SEMI JOIN", "anti": "ANTI JOIN"}[how]
    return f"(SELECT * FROM {_sql(left)} L {join} {_sql(right)} R ON L.{lc} = R.{rc})"


def _rid(shape) -> str:
    # The output key: the ids of every table in the output (a padded side's are NULL, so spelled out).
    return " || '|' || ".join(f"coalesce({t}.{t}_id::VARCHAR, '-')" for t in _out_tables(shape))


@pytest.mark.parametrize("seed", range(40))
def test_join_delta_matches_a_recompute(db, seed, monkeypatch):
    rng = random.Random(seed)
    shapes = list(_shapes(rng))
    states = {t: {} for t in "abcd"}
    for t in "abcd":
        for _ in range(rng.randint(0, 5)):
            _step(rng, states[t], t)
    paths = spy_paths(monkeypatch)
    prev = NEVER
    for run in range(1, 6):
        f = ts(run)
        if run > 1:
            for t in "abcd":
                _step(rng, states[t], t)
        for t in "abcd":
            merge(db, t, sorted(states[t].values()), ", ".join(_cols(t)), f, f"{t}_id",
                  types="INTEGER, INTEGER, INTEGER, INTEGER")
        e = at(db, f, prev)
        for n, shape in enumerate(shapes):
            _builder(e, shape).mutate(rid=_rid(shape)).merge(f"out{n}", pk="rid")
        for n, shape in enumerate(shapes):
            cols = ", ".join(c for t in _out_tables(shape) for c in _cols(t))
            truth = _ordered(db.sql(f"SELECT {cols} FROM {_sql(shape)}").fetchall())
            got = _ordered(T.current_state(db, f"out{n}").query("t", f"SELECT {cols} FROM t").fetchall())
            assert got == truth, (seed, run, shape)
        prev = f
    assert "incremental" in paths  # the runs after the first composed deltas


def test_semi_and_anti_follow_a_key_that_gains_and_loses_its_matches(db):
    """A flip set in isolation: the right side alone changes, crossing zero matches both ways."""
    def r_state(rows_, f):
        merge(db, "r", rows_, "r_id, r_k", f, "r_id")

    merge(db, "l", [(1, 1), (2, 1), (3, 2), (4, None)], "l_id, l_k", ts(1), "l_id")
    for n, (rs, semi, anti) in enumerate([
        ([(1, 1)], [1, 2], [3, 4]),
        ([(1, 1), (2, 1)], [1, 2], [3, 4]),  # a second match: no flip
        ([(2, 1)], [1, 2], [3, 4]),  # one of two removed: still matched
        ([(2, 2)], [3], [1, 2, 4]),  # 1 loses its last match, 2 gains its first
        ([(2, None)], [], [1, 2, 3, 4]),  # a NULL key matches nothing
    ]):
        f, pf = ts(n + 1), (ts(n) if n else NEVER)
        r_state(rs, f)
        e = at(db, f, pf)
        e.source("l", p=1.0).join(e.source("r", p=1.0), on={"l_k": "r_k"}, how="semi").merge("s", pk="l_id")
        e.source("l", p=1.0).join(e.source("r", p=1.0), on={"l_k": "r_k"}, how="anti").merge("a", pk="l_id")
        assert [r[0] for r in rows(db, "s", "l_id")] == semi
        assert [r[0] for r in rows(db, "a", "l_id")] == anti


def test_a_chain_computes_each_intermediate_delta_once(db, monkeypatch):
    """Down a chain where every source changes, the join above reads a child join's delta in two terms; inline,
    the bottom delta would be evaluated 2^depth times. Each is computed once into a temp table, and the tables
    are gone after the write. With only the first source changing each delta is read once and stays inline."""
    from trickle.builder import TrickleBuilder

    made: list[str] = []
    orig = TrickleBuilder._scratch_table
    monkeypatch.setattr(TrickleBuilder, "_scratch_table", lambda self, v: made.append(v) or orig(self, v))

    def sources(f, facts, dim_bump):
        merge(db, "fact", [(i, i % 3, i % 2, i % 5) for i in range(1, facts + 1)], "id, k0, k1, k2", f, "id")
        for j in range(3):
            merge(db, f"d{j}", [(k, k + dim_bump) for k in range(5)], f"k{j}, v{j}", f, f"k{j}")

    def run(f, pf):
        e = at(db, f, pf)
        b = e.source("fact", p=1.0).alias("f")
        for j in range(3):
            b = b.join(e.source(f"d{j}", p=1.0).alias(f"d{j}"), on=f"k{j}")
        b.select("f.id, d0.v0, d1.v1, d2.v2").merge("out", pk="id")

    sources(ts(1), 6, 0)
    run(ts(1), NEVER)
    sources(ts(2), 7, 1)  # every source changes: the two intermediate joins' deltas are materialised
    run(ts(2), ts(1))
    assert len(made) == 2
    truth = sorted(db.sql("SELECT id, v0, v1, v2 FROM fact JOIN d0 USING (k0) JOIN d1 USING (k1) "
                          "JOIN d2 USING (k2)").fetchall())
    assert rows(db, "out", "id, v0, v1, v2") == truth
    assert not db.sql("SELECT table_name FROM duckdb_tables() WHERE table_name LIKE '%dagd%'").fetchall()

    made.clear()
    sources(ts(3), 8, 1)  # only the fact changes: each delta is read once, so none is
    run(ts(3), ts(2))
    assert made == []
