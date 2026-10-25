"""A Sink run's reads of a Source while the Source runs again (plans/trickle-insert-only.md, Stage 0).

Pond Runs pipeline, so a Source can publish its next Run, and fold or compact a merge table, while a Sink run
at an earlier freshness is still reading it. The Sink must read exactly the state as of its own freshness.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb

from duckstring import trickle_io as T
from duckstring.dataplane import ParquetDataPlane

UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


def _merge(con, data_dir, state: dict, f, retain_from=None):
    """One Source Run. ``retain_from`` is what the Catchment sends: the oldest freshness an in-flight Sink run
    of this line may read (at least the line's previous freshness)."""
    values = ", ".join(f"({k}, '{v}')" for k, v in state.items())
    T.merge_table(con, "dim", con.sql(f"SELECT * FROM (VALUES {values}) AS s(id, v)"), f, ("id",))
    ParquetDataPlane().export(con, data_dir, f=f, retain_from=retain_from)


def _as_of(data_dir, f):
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    sql = ParquetDataPlane().read_select(data_dir, "dim", as_of=f)
    return sorted(con.sql(f"SELECT id, v FROM ({sql})").fetchall())


def test_an_as_of_read_survives_its_source_compacting_past_it(tmp_path, monkeypatch):
    """A Sink at f reads the Source as of f after the Source's next Run compacted the table: the rows that Run
    changed must read as they were at f, not vanish."""
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")  # fold and compact eagerly
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    state, history = {k: "v0" for k in range(1, 6)}, {}
    for h in range(1, 20):
        state = {k: f"v{h}" if k == h % 5 + 1 else v for k, v in state.items()}  # update one key per run
        if h == 4:
            state.pop(2)  # and delete one
        _merge(con, data, state, ts(h), retain_from=ts(h - 1) if h > 1 else None)
        history[h] = sorted(state.items())
        if h > 1 and T.load_sidecar(data)["dim"].get("f_base") == ts(h).isoformat():
            break  # this Run compacted
    else:
        raise AssertionError("no Run compacted")
    assert _as_of(data, ts(h)) == history[h]
    assert _as_of(data, ts(h - 1)) == history[h - 1]


def test_a_read_during_a_publish_sees_the_last_committed_run(tmp_path, monkeypatch):
    """The sidecar is the commit marker, written after the files it declares: a read while the next Run's
    files are landing (before its sidecar) reads exactly the last committed Run."""
    import duckstring.dataplane as dp

    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    _merge(con, data, {1: "a", 2: "b"}, ts(1))
    seen = []
    real = dp.publish_plan

    def commit(con_, data_dir, f=None, held=frozenset(), *, write=True):
        if write:  # the files are in place, the sidecar not yet
            seen.append(_as_of(data_dir, None))
        return real(con_, data_dir, f, held, write=write)

    monkeypatch.setattr(dp, "publish_plan", commit)
    _merge(con, data, {1: "A", 3: "c"}, ts(2), retain_from=ts(1))
    assert seen == [[(1, "a"), (2, "b")]]
    assert _as_of(data, None) == [(1, "A"), (3, "c")]


def test_a_read_behind_a_warm_fold_reads_its_own_state(tmp_path, monkeypatch):
    """A warm fold moves change log parts into a band and raises the floor. A Sink run behind it, as of an
    earlier freshness, still reads exactly its state: the folded parts stay until the bound passes."""
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    state, history, folded_at = {k: "v0" for k in range(1, 30)}, {}, None
    for h in range(1, 23):
        state = {k: (f"v{h}" if k % 7 == h % 7 else v) for k, v in state.items()}
        state[100 + h] = f"n{h}"
        _merge(con, data, state, ts(h), retain_from=ts(h - 1) if h > 1 else None)
        history[h] = sorted(state.items())
        bands = T.table_parts(data, "dim__band")
        if h > 2 and bands and max(T.part_f(n) for n in bands) >= ts(h - 1):
            folded_at = h  # this Run folded the previous Run's part into a band
            break
    assert folded_at, "no fold reached the previous Run"
    for h in range(folded_at - 1, folded_at + 1):
        assert _as_of(data, ts(h)) == history[h]


def test_superseded_files_go_once_no_read_can_need_them(tmp_path, monkeypatch):
    """After a compaction the old base, the folded bands and the folded parts stay while a read at an earlier
    freshness may be in flight, and are collected once the bound passes."""
    from duckstring.dataplane import gc_tiers

    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    data = tmp_path / "data"
    state = {k: "v0" for k in range(1, 6)}
    for h in range(1, 20):
        state = {k: (f"v{h}" if k == h % 5 + 1 else v) for k, v in state.items()}
        _merge(con, data, state, ts(h), retain_from=ts(h - 1) if h > 1 else None)
        if h > 1 and T.load_sidecar(data)["dim"].get("f_base") == ts(h).isoformat():
            break
    tokens = {T.part_f(n) for n in T.base_chunks(data, "dim")}
    assert len(tokens) > 1, "the previous base stays for a read behind the compaction"
    gc_tiers(data, ts(h))  # the bound has passed the compaction
    assert {T.part_f(n) for n in T.base_chunks(data, "dim")} == {ts(h)}
    assert all(T.part_f(n) > ts(h) for n in T.table_parts(data, "dim__band"))
    assert _as_of(data, None) == sorted(state.items())


def test_a_draw_never_ships_part_of_a_run(tmp_path, monkeypatch):
    """A Draw taken while a large append Run's chunks are moving into place ships only committed Runs, so the
    consumer's next Draw fetches the whole of the new one."""
    import io
    import shutil
    import zipfile

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.driver import Driver
    from duckstring.catchment.launcher import NoopLauncher
    from duckstring.catchment.registry import pond_data_dir
    from duckstring.catchment.routes import router
    from duckstring.catchment.routes.deploy import _register
    from duckstring.storage import LocalStorage

    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")  # chunks of the 64 KiB floor
    db = connect(tmp_path / "duck.db")
    migrate(db)
    cfg = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "inlet"}
    _register(db, "sales", "1.0.0", "inlet", "ponds/sales/1.0.0", cfg, [{"func": "f1", "name": "r1", "parents": []}])
    driver = Driver(db, tmp_path, "http://x", NoopLauncher())
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.driver, app.state.root, app.state.db = driver, driver.root, driver.db
    client = TestClient(app)

    data = pond_data_dir(tmp_path, "sales", 1)
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    cons = tmp_path / "consumer"

    def run(f, lo, hi, retain_from=None):
        T.append_table(con, "events", con.sql(f"SELECT range AS id, md5(range::VARCHAR) AS v FROM range({lo}, {hi})"),
                       f, ("id",), fail_on_conflict=False)
        ParquetDataPlane().export(con, data, f=f, retain_from=retain_from)

    def draw():
        after = T.landed_after(cons) if cons.exists() else None
        resp = client.get("/api/draw/sales/1", params={"after": after} if after else {})
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                (cons / name).parent.mkdir(parents=True, exist_ok=True)
                (cons / name).write_bytes(zf.read(name))

    run(ts(1), 0, 100_000)
    draw()
    real, moves = LocalStorage.move_into, []

    def move_then_draw(self, dest, src_name, dest_name):
        real(self, dest, src_name, dest_name)
        moves.append(dest_name)
        if len(moves) == 1:
            draw()  # mid-publish: one chunk of the ts(2) Run is in place

    monkeypatch.setattr(LocalStorage, "move_into", move_then_draw)
    run(ts(2), 100_000, 300_000, retain_from=ts(1))
    monkeypatch.setattr(LocalStorage, "move_into", real)
    assert len(moves) > 1, "the ts(2) Run should be several chunks"
    draw()
    got = duckdb.connect().sql(
        f"SELECT count(*), count(DISTINCT id) FROM read_parquet('{cons}/events/*.parquet')").fetchall()[0]
    assert got == (300_000, 300_000)
    shutil.rmtree(cons)
