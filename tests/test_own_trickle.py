"""A builder starting from a table another Ripple of the same Pond writes (plans/showcase-fixes.md item 13).
``pond.trickle("own_table")`` wasn't supported, so the attention showcase moved an aggregate into the next Pond
only to read a table its own Pond had just written. The changes come from the registry over the reading Ripple's
window, and the state as of the Run's freshness, so a writer already running the next Run doesn't leak into it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring import agg
from duckstring.core import Pond, RippleOrderError
from duckstring.engine.core import NEVER

F = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i) for i in range(6)]
SCOPE = {"table_writers": {"post": "load"}}


def _handle(con, root, ripple, f, previous_f=NEVER, ancestors=()):
    return Pond("chatter", "1.0.0", con, root, f=f, previous_f=previous_f, ripple=ripple,
                ancestors=set(ancestors), **SCOPE)


def _load(con, root, f, posts):
    """The writer Ripple: the complete current set of posts, as a merge table."""
    rows = ", ".join(f"({i}, '{t}', {n})" for i, t, n in posts)
    _handle(con, root, "load", f).merge_table(
        "post", con.sql(f"SELECT * FROM (VALUES {rows}) v(id, topic, words)"), pk="id")


def _hourly(con, root, f, previous_f):
    """The reading Ripple: an incremental aggregate over the writer's table."""
    p = _handle(con, root, "hourly", f, previous_f, ancestors=["load"])
    p.trickle("post").aggregate(by="topic", n=agg.count(), words=agg.sum("words")).merge("per_topic")
    return sorted(p.read_table("per_topic").fetchall())


def _expected(posts):
    out: dict[str, list[int]] = {}
    for _, t, n in posts:
        out.setdefault(t, [0, 0])
        out[t][0] += 1
        out[t][1] += n
    return sorted((t, c, w) for t, (c, w) in out.items())


def test_a_builder_reads_a_parent_ripples_table_incrementally(tmp_path, monkeypatch):
    con = duckdb.connect(str(tmp_path / "registry.duckdb"))
    posts = [(i, ["rain", "trains", "snow"][i % 3], i) for i in range(1, 31)]
    _load(con, tmp_path, F[0], posts)
    assert [tuple(r) for r in _hourly(con, tmp_path, F[0], NEVER)] == _expected(posts)

    from duckstring.trickle import io as T

    paths = []
    real = T.apply_aggregate
    monkeypatch.setattr(T, "apply_aggregate", lambda *a, **k: (paths.append(a[4]), real(*a, **k))[1])
    posts = [p for p in posts if p[0] != 2] + [(31, "trains", 1)]  # one removed, one added
    posts = [(i, t, 99) if i == 3 else (i, t, n) for i, t, n in posts]  # one changed
    _load(con, tmp_path, F[1], posts)
    assert [tuple(r) for r in _hourly(con, tmp_path, F[1], F[0])] == _expected(posts)
    assert paths == ["incremental"]  # read the changes, not the whole table


def test_a_writer_running_ahead_doesnt_leak_into_the_readers_run(tmp_path):
    """Pond Runs pipeline: the writer can finish the next Run before the reader runs this one."""
    con = duckdb.connect(str(tmp_path / "registry.duckdb"))
    first = [(1, "rain", 3)]
    _load(con, tmp_path, F[0], first)
    _hourly(con, tmp_path, F[0], NEVER)
    at_1 = [(1, "rain", 3), (2, "snow", 4)]
    at_2 = [(2, "snow", 9), (5, "rain", 1)]
    _load(con, tmp_path, F[1], at_1)
    _load(con, tmp_path, F[2], at_2)  # the writer is a Run ahead
    assert [tuple(r) for r in _hourly(con, tmp_path, F[1], F[0])] == _expected(at_1)
    assert [tuple(r) for r in _hourly(con, tmp_path, F[2], F[1])] == _expected(at_2)


def test_the_writer_must_be_a_parent_and_must_have_written(tmp_path):
    con = duckdb.connect(str(tmp_path / "registry.duckdb"))
    stranger = Pond("chatter", "1.0.0", con, tmp_path, f=F[0], ripple="hourly", ancestors=set(), **SCOPE)
    with pytest.raises(RippleOrderError, match="'load' isn't among its parents"):
        stranger.read_delta("post")
    early = _handle(con, tmp_path, "hourly", F[0], ancestors=["load"])
    with pytest.raises(RippleOrderError, match="hasn't been written"):
        early.read_delta("post")
