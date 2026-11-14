"""A Source with many Runs published (plans/showcase-fixes.md item 0): what a Sink's Run reads must not grow with
the number of Runs before it. Before, a delta read listed and opened every part the Source had published, and
resolving a builder's join keys bound them all too; the transit showcase's Runs slowed by half a millisecond
with each Run."""

from __future__ import annotations

import random
from datetime import timedelta

import duckdb
import pytest
from test_change_detection import F1, Producer, _sink

from duckstring.dataplane import ParquetDataPlane
from duckstring.storage import LocalStorage

pytestmark = pytest.mark.timeout(120)


def _at(i: int):
    return F1 + timedelta(minutes=i)


def _publish_runs(tmp_path, runs: int, rng: random.Random) -> Producer:
    """A Source Pond: an append history (some Runs empty) and a merge table changing on some Runs, with eager
    folds (a 1-byte threshold) so the merge's change log moves into bands as it goes."""
    prod = Producer(tmp_path)
    state = {k: 0 for k in range(8)}
    for i in range(runs):
        p = prod.handle(_at(i))
        n = rng.choice([0, 0, 3, 7])
        p.append_table("events", p.con.sql(f"SELECT {i * 10} + range AS id, range % 4 AS g FROM range({n})"), pk="id")
        if rng.random() < 0.4:
            state[rng.randrange(8)] = i
            if rng.random() < 0.2:
                state.pop(rng.choice(list(state)), None)
        rows = ", ".join(f"({k}, {v})" for k, v in state.items()) or "(NULL, NULL)"
        rel = p.con.sql(f"SELECT * FROM (VALUES {rows}) s(k, v) WHERE k IS NOT NULL")
        p.merge_table("dim", rel, pk="k", compact_threshold=1)
        prod.publish(_at(i), retain_from=_at(i - 3) if i >= 3 else None)
    return prod


def _zset(delta):
    return sorted(delta.zset.fetchall(), key=repr), delta.is_full


def test_a_windowed_delta_reads_the_same_rows_as_the_whole_history(tmp_path, monkeypatch):
    _publish_runs(tmp_path, 40, random.Random(7))
    windows = [(_at(a), _at(b)) for a, b in [(30, 31), (30, 39), (36, 37), (38, 39), (35, 39)]]
    for table in ("events", "dim"):
        new = [_zset(_sink(tmp_path, f, pf).read_delta(f"src.{table}")) for pf, f in windows]
        with monkeypatch.context() as m:
            m.delattr(ParquetDataPlane, "read_window")  # the earlier read: the whole history, filtered
            old = [_zset(_sink(tmp_path, f, pf).read_delta(f"src.{table}")) for pf, f in windows]
        assert new == old, table
        assert any(rows and not full for rows, full in new), table  # real changes, read as a window


def test_a_delta_read_names_only_the_windows_parts(tmp_path, monkeypatch):
    prod = _publish_runs(tmp_path, 30, random.Random(3))
    seen: list[str] = []
    real = duckdb.DuckDBPyConnection.sql

    def spy(self, query, *a, **k):
        seen.append(query)
        return real(self, query, *a, **k)

    monkeypatch.setattr(duckdb.DuckDBPyConnection, "sql", spy)
    sink = _sink(tmp_path, _at(29), _at(28))
    sink.read_delta("src.events").zset.fetchall()
    events = LocalStorage(prod.data_dir.root / "events")
    named = {n for n in events.parquet_names() if any(n in q for q in seen)}
    assert len(events.parquet_names()) > 10 and len(named) <= 1


def test_resolving_a_sources_columns_binds_one_part(tmp_path):
    prod = _publish_runs(tmp_path, 20, random.Random(5))
    dp = ParquetDataPlane()
    sql = dp.columns_select(prod.data_dir, "events", as_of=_at(19))
    assert sql.count(".parquet") == 1
    assert _sink(tmp_path, _at(19), _at(18)).source_columns("src.events") == ["id", "g"]
