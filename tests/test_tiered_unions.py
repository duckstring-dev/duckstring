"""Tiered unions of per-run parts (plans/showcase-fixes.md item 4, redesigned 2026-11-20).

Each Run that changes a Trickle table adds a file to its parts directory. A union concatenates consecutive files
into one and consolidates nothing: every row keeps its ``_duckstring_f``, which every read selects rows by. So a
read at any freshness, and a delta over any window, reads the same rows from the unions as from the separate
files, and the floor never moves.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring import trickle_io as T
from duckstring.dataplane import ParquetDataPlane, gc_tiers, persist_tree, plan_unions
from duckstring.storage import get_storage
from duckstring.trickle.context import NEVER

UTC = timezone.utc


def ts(i: int) -> datetime:
    return datetime(2026, 6, 16, tzinfo=UTC) + timedelta(minutes=i)


@pytest.fixture(autouse=True)
def _small_tiers(monkeypatch):
    monkeypatch.setenv("DUCKSTRING_TIER_FILES", "4")


def _reader():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


def _rows(sql: str) -> list:
    return sorted(_reader().sql(f"SELECT * EXCLUDE (_duckstring_f) FROM ({sql})").fetchall())


def _as_of(data, table, f) -> list:
    return _rows(ParquetDataPlane().read_select(data, table, as_of=f))


def _delta(data, table, previous_f, f) -> list:
    con = _reader()
    d = T.read_delta(con, data, table, previous_f, f, dp=ParquetDataPlane())
    return sorted(d.zset.fetchall()), d.is_full


def _append_run(con, data, i, retain_from=None, **kw):
    """One Run of an append table adding two rows."""
    rel = con.sql(f"SELECT {2 * i} AS id, 'r{i}' AS v UNION ALL SELECT {2 * i + 1}, 'r{i}'")
    T.append_table(con, "ev", rel, ts(i), ("id",), **kw)
    ParquetDataPlane().export(con, data, f=ts(i), **({} if retain_from is None else {"retain_from": retain_from}))


def _names(data, table) -> list[str]:
    return T.table_parts(data, table)


# ─── naming and the file set a read uses ───────────────────────────────────────


def test_a_union_is_named_by_its_range_and_the_publish_that_wrote_it():
    name = T.range_name(ts(1), ts(5), ts(9))
    assert T.is_union(name) and not T.is_chunk(name)
    assert (T.part_lo(name), T.part_f(name), T.part_made(name)) == (ts(1), ts(5), ts(9))
    single, chunk = T.part_name(ts(3)), T.part_name(ts(3)).replace(".parquet", "__1.parquet")
    assert T.part_lo(single) == T.part_f(single) == ts(3) and T.part_made(single) is None
    assert T.is_chunk(chunk) and T.part_f(chunk) == ts(3)


def test_a_read_uses_the_widest_files_and_never_a_row_twice():
    parts = [T.part_name(ts(i)) for i in range(1, 7)]
    inner = T.range_name(ts(1), ts(3), ts(4))
    outer = T.range_name(ts(1), ts(5), ts(8))
    assert T.live_parts(parts + [inner]) == [inner] + parts[3:]
    assert T.live_parts(parts + [inner, outer]) == [outer, parts[5]]
    # Sorted by the newest freshness each holds: the newest file is last.
    assert T.live_parts([parts[5], outer]) == [outer, parts[5]]


def test_tiers_union_runs_of_small_files_and_never_a_chunk_or_a_large_file():
    names = [T.part_name(ts(i)) for i in range(1, 11)]
    sizes = dict.fromkeys(names, 10)
    groups = plan_unions(names, sizes, f=ts(20), cap=1000, fan=4)
    assert groups == [names]  # one run of ten tier-0 files, packed within the cap
    # A chunk of a larger Run breaks the run: fewer than four on one side are left alone.
    chunk = T.part_name(ts(4)).replace(".parquet", "__1.parquet")
    with_chunk = names[:3] + [chunk] + names[3:]
    sizes[chunk] = 10
    assert plan_unions(with_chunk, sizes, f=ts(20), cap=1000, fan=4) == [names[3:]]
    # Nothing at or past the publish's own Run, or past the bound, or below the floor.
    assert plan_unions(names, sizes, f=ts(5), cap=1000, fan=4) == [names[:4]]
    assert plan_unions(names, sizes, f=ts(20), bound=ts(6), cap=1000, fan=4) == [names[:6]]
    assert plan_unions(names, sizes, f=ts(20), floor=ts(8), cap=1000, fan=4) == []
    # Tier 1: files under the cap but over cap/fan join only a run of their own size or smaller.
    big = dict.fromkeys(names, 300)
    assert plan_unions(names, big, f=ts(20), cap=1000, fan=4) == [names[0:3], names[3:6], names[6:9]]
    assert plan_unions(names, dict.fromkeys(names, 1000), f=ts(20), cap=1000, fan=4) == []


# ─── an append table ──────────────────────────────────────────────────────────


def test_an_append_tables_parts_are_unioned_and_every_read_matches(tmp_path):
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    expected = {}
    for i in range(1, 41):
        _append_run(con, data, i)
        expected[i] = sorted(r for j in range(1, i + 1) for r in ((2 * j, f"r{j}"), (2 * j + 1, f"r{j}")))
    names = _names(data, "ev")
    assert any(T.is_union(n) for n in names)
    assert len(names) < 12  # forty Runs in a few files (the constituents were collected: no reader to protect)
    assert T.load_sidecar(data)["ev"]["format_version"] == 2
    for i in (1, 7, 16, 23, 40):  # a read at a freshness inside a union reads up to it
        assert _as_of(data, "ev", ts(i)) == expected[i]
    for a, b in ((0, 3), (3, 17), (16, 18), (5, 40), (39, 40)):  # windows straddling unions at either end
        rows, full = _delta(data, "ev", ts(a) if a else NEVER, ts(b))
        want = [(r[0], r[1], 1) for r in expected[b] if a == 0 or r not in expected[a]]
        assert rows == sorted(want)
        assert full == (a == 0)
    reg = duckdb.connect()
    for a, b in ((3, 17), (16, 18), (17, 17)):
        assert T.window_changed(reg, data, "ev", ts(a), ts(b)) == (a < b)


def test_a_table_that_never_unions_stays_format_1(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_TIER_FILES", "0")
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    for i in range(1, 10):
        _append_run(con, data, i)
    assert not any(T.is_union(n) for n in _names(data, "ev"))
    assert T.load_sidecar(data)["ev"]["format_version"] == 1


def test_unions_stay_below_retain_from_and_constituents_wait_for_the_bound(tmp_path):
    """A union takes only files at or below the Catchment's retain_from (a replay or pipelined Run of the
    Pond's own can't write into it), and its files stay until the bound passes the publish that wrote it: a read
    planned before then may name them."""
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    for i in range(1, 5):
        _append_run(con, data, i, retain_from=ts(i - 1) if i > 1 else NEVER)
    assert not any(T.is_union(n) for n in _names(data, "ev"))  # three files at or below ts(3): too few
    planned = ParquetDataPlane().read_select(data, "ev", as_of=ts(4))  # a Sink's read, planned now
    named = set(T.live_parts(_names(data, "ev")))
    for i in range(5, 10):  # the Sink is still running: retain_from stays at its pin
        _append_run(con, data, i, retain_from=ts(4))
    unions = [n for n in _names(data, "ev") if T.is_union(n)]
    assert unions and all(T.part_f(n) <= ts(4) for n in unions)
    assert named <= set(_names(data, "ev"))  # every file the planned read names is still there
    assert len(_rows(planned)) == 8
    _append_run(con, data, 10, retain_from=ts(9))  # the Sink finished: the bound passes the unions' publishes
    assert not named & set(_names(data, "ev"))  # the files the first union holds are collected
    assert _as_of(data, "ev", ts(4)) == sorted(
        r for j in range(1, 5) for r in ((2 * j, f"r{j}"), (2 * j + 1, f"r{j}")))


def test_a_floor_raised_inside_a_union_hides_the_rows_below_it(tmp_path):
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    for i in range(1, 13):
        _append_run(con, data, i)
    assert any(T.is_union(n) and T.part_lo(n) < ts(4) < T.part_f(n) for n in _names(data, "ev"))
    _append_run(con, data, 13, retain_n=10)  # retention: the floor moves inside a union
    floor = datetime.fromisoformat(T.load_sidecar(data)["ev"]["floor"])
    assert any(T.is_union(n) and T.part_lo(n) < floor <= T.part_f(n) for n in _names(data, "ev"))
    got = _as_of(data, "ev", None)
    reg = sorted(r[:2] for r in con.execute('SELECT id, v FROM "ev"').fetchall())
    assert got == reg  # exactly what retention kept


# ─── a merge table's change log ───────────────────────────────────────────────


def _merge_run(con, data, state, i, **kw):
    values = ", ".join(f"({k}, '{v}')" for k, v in state.items())
    T.merge_table(con, "dim", con.sql(f"SELECT * FROM (VALUES {values}) AS s(id, v)"), ts(i), ("id",))
    ParquetDataPlane().export(con, data, f=ts(i), **kw)


def test_a_merge_change_log_is_unioned_and_reads_match(tmp_path):
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    state, history = {k: "v0" for k in range(1, 8)}, {}
    for i in range(1, 31):
        state = {k: (f"v{i}" if k == i % 7 + 1 else v) for k, v in state.items()}
        _merge_run(con, data, state, i)
        history[i] = sorted(state.items())
    clog = T.changelog_name("dim")
    assert any(T.is_union(n) for n in _names(data, clog))
    assert len(_names(data, clog)) < 12
    for i in (1, 6, 13, 22, 30):
        assert _as_of(data, "dim", ts(i)) == history[i]
    rows, full = _delta(data, "dim", ts(13), ts(22))
    assert not full
    changed = {k for k, v in history[22] if (k, v) not in history[13]}
    assert sorted(r[0] for r in rows if r[2] > 0) == sorted(changed)


def test_a_retention_fold_cutting_through_a_change_log_union_reads_once(tmp_path):
    """A retention fold moves change log rows up to an arbitrary freshness into a band, which may fall inside a
    union: the union's rows at or below the band are read from the band only."""
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    state, history = {k: "v0" for k in range(1, 8)}, {}
    for i in range(1, 16):
        state = {k: (f"v{i}" if k == i % 7 + 1 else v) for k, v in state.items()}
        _merge_run(con, data, state, i)
        history[i] = sorted(state.items())
    clog = T.changelog_name("dim")
    union = next(n for n in _names(data, clog) if T.is_union(n))
    cut = T.part_lo(union) + (T.part_f(union) - T.part_lo(union)) / 2
    T.fold_warm(con, "dim", cut)  # what retention does, at its own cutoff
    state[1] = "last"
    _merge_run(con, data, state, 16)
    history[16] = sorted(state.items())
    assert any(T.part_lo(n) <= cut < T.part_f(n) for n in _names(data, clog) if T.is_union(n))
    assert _as_of(data, "dim", ts(16)) == history[16]
    assert _as_of(data, "dim", ts(15)) == history[15]
    count_sql = ParquetDataPlane().consolidated_count_select(data, "dim", T.load_sidecar(data)["dim"], as_of=ts(16))
    assert _reader().sql(count_sql).fetchall()[0][0] == len(history[16])


# ─── copies: registry recovery, the Persist mirror, Draws ─────────────────────


def test_a_registry_rebuilt_from_the_published_state_holds_every_row_once(tmp_path):
    from duckstring.dataplane import hydrate_registry

    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    for i in range(1, 15):
        _append_run(con, data, i)
    fresh = duckdb.connect(str(tmp_path / "fresh.duckdb"))
    hydrate_registry(fresh, get_storage(str(data)))
    assert sorted(fresh.execute('SELECT id, v FROM "ev"').fetchall()) == sorted(
        con.execute('SELECT id, v FROM "ev"').fetchall())
    assert T.read_meta(fresh)["ev"]["format_version"] == 2  # still says it holds unions
    _append_run(fresh, data, 15)  # and publishes on from there without rewriting a unioned freshness
    assert _as_of(data, "ev", None) == sorted(r[:2] for r in fresh.execute('SELECT id, v FROM "ev"').fetchall())


def test_the_persist_mirror_drops_what_a_union_holds(tmp_path):
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data, durable = tmp_path / "data", tmp_path / "durable"
    for i in range(1, 4):
        _append_run(con, data, i)
    persist_tree(get_storage(str(data)), get_storage(str(durable)))
    for i in range(4, 12):
        _append_run(con, data, i)
        persist_tree(get_storage(str(data)), get_storage(str(durable)))
    assert _names(durable, "ev") == _names(data, "ev")
    assert _as_of(durable, "ev", None) == _as_of(data, "ev", None)


def test_a_consumer_holding_a_unions_files_reads_each_row_once_and_collects_them(tmp_path):
    """A Draw ships the files above what the consumer holds, which may be a union of files it already has."""
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data, consumer = tmp_path / "data", tmp_path / "consumer"
    for i in range(1, 4):
        _append_run(con, data, i)
    shutil.copytree(data, consumer)  # the consumer landed Runs 1 to 3 as single parts
    for i in range(4, 12):
        _append_run(con, data, i)
    after = datetime.fromisoformat(T.landed_after(consumer))
    for n in T.live_parts(_names(data, "ev")):  # what the Draw route ships: files ending past `after`
        if T.part_f(n) > after:
            shutil.copy(data / "ev" / n, consumer / "ev" / n)
    shutil.copy(data / "_trickle.json", consumer / "_trickle.json")
    assert any(T.part_lo(n) <= after for n in _names(consumer, "ev") if T.is_union(n))
    assert _as_of(consumer, "ev", None) == _as_of(data, "ev", None)
    gc_tiers(get_storage(str(consumer)))
    assert _names(consumer, "ev") == T.live_parts(_names(consumer, "ev"))


def test_the_spout_mirror_never_holds_a_union_and_its_files(tmp_path):
    from duckstring.egress.base import get_egress

    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data, dest = tmp_path / "data", tmp_path / "mirror"
    driver = get_egress(f"file://{dest}")
    for i in range(1, 12):
        _append_run(con, data, i, retain_from=ts(i - 1) if i > 1 else NEVER)
        driver.mirror(get_storage(str(data)), "ev", T.load_sidecar(data)["ev"], f=ts(i))
    assert _names(dest, "ev") == T.live_parts(_names(dest, "ev"))
    assert sorted(_reader().sql(f"SELECT id, v FROM read_parquet('{dest}/ev/*.parquet')").fetchall()) == \
        _as_of(data, "ev", None)
