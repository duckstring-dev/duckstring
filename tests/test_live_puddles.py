"""Live and replayable Puddles (plans/showcase-fixes.md item 10): Puddle sets, the handle's write methods,
``pond hydrate --live`` / ``--as-at``, ``@puddle.step`` and ``pond run --incremental`` with its targets.

"Production" here is a Catchment root a test publishes into with the data plane, served by a real Catchment and
registered as ``prod``: Puddles read it through its query API, never in place. Nothing is deployed on it, so it has
no run history, and positions come from the Sources' change points."""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from duckstring.cli import app
from duckstring.local import hydrate, load_project, run_pond

pytestmark = pytest.mark.timeout(30)

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def t(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


# ── production ───────────────────────────────────────────────────────────────


class Prod:
    """A Catchment root that Sources publish into, one Pond Run at a time."""

    def __init__(self, root: Path):
        self.root = root

    def publish(self, source: str, f: datetime, build, major: int = 1) -> None:
        from duckstring.core import Pond
        from duckstring.dataplane import ParquetDataPlane

        line = self.root / "ponds" / source / f"m{major}"
        data = line / "data"
        line.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(line / "registry.duckdb"))
        try:
            pond = Pond(source, f"{major}.0.0", con, root=self.root, f=f, sources=[], own_data_dir=data,
                        staging_dir=line / ".staging")
            build(pond)
            ParquetDataPlane().export(con, data, f=f, exact_ints=True)
        finally:
            con.close()


@pytest.fixture
def prod(tmp_path):
    from catchment_server import serve_catchment

    from duckstring.cli.config import register_catchment, set_default_catchment

    root = tmp_path / "prod"
    root.mkdir()
    with serve_catchment(root) as url:
        register_catchment("prod", url=url, kind="local")
        set_default_catchment("prod")
        yield Prod(root)


def events(ids):
    """A build that appends ``ids`` to ``tx.event`` (``id``, ``value = id * 10``)."""
    def build(pond):
        rows = ", ".join(f"({i}, {i * 10})" for i in ids)
        pond.append_table("event", pond.con.sql(f"SELECT * FROM (VALUES {rows}) v(id, value)"), pk="id")
    return build


def prices(rows):
    """A build that sets ``dim.price`` to ``rows`` (``k``, ``p``)."""
    def build(pond):
        vals = ", ".join(f"({k}, {p})" for k, p in rows)
        pond.merge_table("price", pond.con.sql(f"SELECT * FROM (VALUES {vals}) v(k, p)"), pk="k")
    return build


# ── the Pond under test ──────────────────────────────────────────────────────

_POND = """
    from duckstring import ripple

    @ripple
    def count(pond):
        delta = pond.read_delta("tx.event")
        pond.read_table("tx.event")  # registers the view `event`
        pond.write_table("seen", pond.con.sql(
            f"SELECT count(*) AS n, coalesce(sum(value), 0) AS total, "
            f"'{pond.f.isoformat()}' AS f, '{pond.previous_f.isoformat()}' AS prev FROM event"))
        n = delta.zset.aggregate("count(*)").fetchall()[0][0]
        pond.write_table("window", pond.con.sql(f"SELECT {n} AS n, {str(delta.is_full).lower()} AS is_full"))
"""


def make_pond(path: Path, puddles: str = "from duckstring import puddle\n", pond: str = _POND,
              sources: dict | None = None, toml: str | None = None) -> Path:
    sources = {"tx": "1.0.0"} if sources is None else sources
    text = '[pond]\nname = "counter"\nversion = "0.1.0"\n'
    if sources:
        text += "\n[sources]\n" + "".join(f'{s} = "{v}"\n' for s, v in sources.items())
    (path / "pond.toml").write_text(text)
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "pond.py").write_text(textwrap.dedent(pond))
    (path / "src" / "puddles.py").write_text(textwrap.dedent(puddles))
    if toml is not None:
        (path / "puddles.toml").write_text(textwrap.dedent(toml))
    return path


def out(project, table: str) -> list[tuple]:
    from duckstring.dataplane import ParquetDataPlane

    select = ParquetDataPlane().read_select(project.out_dir, table)
    con = duckdb.connect()
    cols = [c for c in con.sql(select).columns if not c.startswith("_duckstring_")]
    return con.sql(f"SELECT {', '.join(cols)} FROM ({select})").fetchall()


def seen(project) -> dict:
    n, total, f, prev = out(project, "seen")[0]
    return {"n": n, "total": total, "f": datetime.fromisoformat(f), "prev": prev}


def ok(result):
    assert result.ok, [(r.name, r.error, r.traceback) for r in result.ripples]
    return result


# ── Puddle sets ──────────────────────────────────────────────────────────────

_SETS = """
    from duckstring import puddle

    @puddle("tx.event")
    def full(p):
        return p.con.sql("SELECT range AS id, range * 10 AS value FROM range(10)")

    @puddle("tx.event", set="small")
    def small(p):
        return p.con.sql("SELECT range AS id, range * 10 AS value FROM range(2)")

    @puddle("other.thing")
    def thing(p):
        return p.con.sql("SELECT 1 AS x")
"""


def test_a_set_replaces_the_default_where_it_has_a_definition(tmp_path):
    project = load_project(make_pond(tmp_path, _SETS, sources={"tx": "1.0.0", "other": "1.0.0"}))
    ok_results, _ = hydrate(project, set_name="small")
    assert {r.name for r in ok_results} == {"small", "thing"}  # other falls back to the default set
    assert ok(run_pond(project)) and seen(project)["n"] == 2


def test_puddles_toml_local_picks_a_set_per_source(tmp_path):
    project = load_project(make_pond(tmp_path, _SETS, sources={"tx": "1.0.0", "other": "1.0.0"},
                                     toml='[local]\ntx = "small"\n'))
    hydrate(project)
    ok(run_pond(project))
    assert seen(project)["n"] == 2
    hydrate(project, set_name="default")  # --set overrides the file
    ok(run_pond(project))
    assert seen(project)["n"] == 10


def test_an_unknown_set_or_source_is_refused(tmp_path):
    project = load_project(make_pond(tmp_path, _SETS, sources={"tx": "1.0.0", "other": "1.0.0"}))
    with pytest.raises(ValueError, match="no Puddle set 'tiny'"):
        hydrate(project, set_name="tiny")
    (tmp_path / "puddles.toml").write_text('[local]\nnope = "small"\n')
    with pytest.raises(ValueError, match="'nope'"):
        hydrate(project)


def test_two_definitions_for_one_target_in_a_set_raise(tmp_path):
    puddles = """
        from duckstring import puddle

        @puddle("tx.event", set=("small", "big"))
        def a(p):
            return p.con.sql("SELECT 1 AS id, 1 AS value")

        @puddle("tx.event", set="big")
        def b(p):
            return p.con.sql("SELECT 2 AS id, 2 AS value")
    """
    project = load_project(make_pond(tmp_path, puddles))
    with pytest.raises(ValueError, match="two Puddle definitions for 'tx.event' in the set 'big'"):
        hydrate(project)


def test_default_is_not_a_set_label(tmp_path):
    from duckstring import puddle

    with pytest.raises(ValueError, match="leave set= out"):
        puddle("tx.event", set="default")


# ── write methods ────────────────────────────────────────────────────────────


def test_the_handle_writes_trickle_layouts(tmp_path):
    """p.append_table and p.merge_table give the Puddle the layout incremental reads need, without the data plane by
    hand (the old demo pattern)."""
    puddles = """
        from duckstring import puddle

        @puddle("tx")
        def tx(p):
            p.append_table("event", p.con.sql("SELECT range AS id, range * 10 AS value FROM range(3)"), pk="id")
            p.merge_table("dim", p.con.sql("SELECT 1 AS k, 'a' AS v"), pk="k")
    """
    project = load_project(make_pond(tmp_path, puddles))
    hydrate(project)
    from duckstring.trickle.io import load_sidecar, sidecar_tables

    entries = sidecar_tables(load_sidecar(project.snapshot_dir("tx")))
    assert entries["event"]["mode"] == "append" and entries["dim"]["mode"] == "merge"
    ok(run_pond(project))
    assert seen(project)["n"] == 3
    assert out(project, "window") == [(3, True)]  # a baseline reads its Sources in full


# ── nothing hydrated ─────────────────────────────────────────────────────────


def test_a_run_with_nothing_hydrated_names_hydrate(tmp_path):
    project = load_project(make_pond(tmp_path))
    with pytest.raises(ValueError, match="pond hydrate"):
        run_pond(project)


def test_an_incremental_run_needs_a_baseline(tmp_path):
    project = load_project(make_pond(tmp_path, _SETS, sources={"tx": "1.0.0", "other": "1.0.0"}))
    hydrate(project)
    with pytest.raises(ValueError, match="duckstring pond run first"):
        run_pond(project, incremental=True)


# ── live ─────────────────────────────────────────────────────────────────────


def test_live_reads_a_source_in_place_as_of_the_hydration(tmp_path, prod):
    prod.publish("tx", t(1), events([1, 2]))
    prod.publish("tx", t(2), events([3]))
    project = load_project(make_pond(tmp_path))
    results, _ = hydrate(project, live=True)
    assert [r.status for r in results] == ["ok"]
    assert not project.snapshot_dir("tx").exists()  # nothing copied

    ok(run_pond(project))
    first = seen(project)
    assert (first["n"], first["f"]) == (3, t(2))

    prod.publish("tx", t(3), events([4, 5]))
    ok(run_pond(project))  # the baseline stays as hydrated
    assert seen(project)["n"] == 3

    ok(run_pond(project, incremental=True))  # follows production forward
    after = seen(project)
    assert (after["n"], after["f"], after["prev"]) == (5, t(3), t(2).isoformat())
    assert out(project, "window") == [(2, False)]  # just the new rows


def test_a_live_definition_filters_each_window(tmp_path, prod):
    prod.publish("tx", t(1), events([1, 2, 3, 4]))
    puddles = """
        from duckstring import puddle

        @puddle("tx.event")
        def evens(p):
            return p.read_source().filter("id % 2 = 0")
    """
    project = load_project(make_pond(tmp_path, puddles))
    results, _ = hydrate(project, live=True)
    assert results[0].note == "live, through its definition"
    ok(run_pond(project))
    assert seen(project)["n"] == 2

    prod.publish("tx", t(2), events([5, 6, 8]))
    ok(run_pond(project, incremental=True))
    assert seen(project)["n"] == 4
    assert out(project, "window") == [(2, False)]  # 6 and 8, as an append delta

    ok(run_pond(project))  # back to the baseline
    assert seen(project)["n"] == 2


def test_a_live_filter_runs_on_the_sources_catchment(tmp_path, prod, monkeypatch):
    """The definition's query is what travels: the filter is in the SQL sent, and only its rows come back."""
    from duckstring.remote_source import HttpTransport

    sent, rows = [], []
    real = HttpTransport.query

    def spy(self, payload):
        content, warning = real(self, payload)
        if "LIMIT 0" not in payload["sql"]:
            sent.append(payload["sql"])
            path = tmp_path / "r.parquet"
            path.write_bytes(content)
            rows.append(duckdb.sql(f"SELECT count(*) FROM '{path}'").fetchall()[0][0])
        return content, warning

    monkeypatch.setattr(HttpTransport, "query", spy)
    prod.publish("tx", t(1), events(range(1, 101)))
    puddles = """
        from duckstring import puddle

        @puddle("tx.event")
        def few(p):
            return p.read_source().filter("id <= 3").project("id, value")
    """
    project = load_project(make_pond(tmp_path, puddles))
    hydrate(project, live=True)
    ok(run_pond(project))
    assert seen(project)["n"] == 3
    assert sent and all("id <= 3" in q for q in sent)
    assert max(rows) == 3


def test_a_definition_that_leaves_the_query_is_refused(tmp_path, prod):
    prod.publish("tx", t(1), events([1, 2]))
    puddles = """
        from duckstring import puddle

        @puddle("tx.event")
        def fetched(p):
            rows = p.read_source().to_arrow_table()  # Arrow, not .df(): numpy isn't a dependency
            return p.con.from_arrow(rows.slice(0, 1))
    """
    project = load_project(make_pond(tmp_path, puddles))
    results, _ = hydrate(project, live=True)
    assert results[0].status == "failed"
    assert "query over the Source alone" in results[0].error


def test_a_generated_definition_is_copied_under_live(tmp_path, prod):
    prod.publish("tx", t(1), events([1, 2]))
    puddles = """
        from duckstring import puddle

        @puddle("tx.event")
        def generated(p):
            return p.con.sql("SELECT range AS id, range * 10 AS value FROM range(10)")
    """
    project = load_project(make_pond(tmp_path, puddles))
    results, warnings = hydrate(project, live=True)
    assert all(r.status == "ok" for r in results), results
    assert any("copied as written" in w for w in warnings)
    ok(run_pond(project))
    assert seen(project)["n"] == 10


def test_live_production_short_of_the_target_refuses(tmp_path, prod):
    prod.publish("tx", t(1), events([1]))
    project = load_project(make_pond(tmp_path))
    hydrate(project, live=True)
    ok(run_pond(project))
    with pytest.raises(ValueError, match="production hasn't brought 'tx'"):
        run_pond(project, incremental=True, to=t(5))


def test_a_source_with_nothing_published_is_refused(tmp_path, prod):
    project = load_project(make_pond(tmp_path))
    results, _ = hydrate(project, live=True)
    assert results[0].status == "failed"
    assert "published nothing" in results[0].error


def test_nothing_is_read_in_place(tmp_path, prod, monkeypatch):
    """Every read goes through the Catchment's API: with the data plane's file reads blocked for production's
    root, a live hydration and its runs still work."""
    from duckstring import dataplane

    prod.publish("tx", t(1), events([1, 2]))
    prod.publish("tx", t(2), events([3]))
    real = dataplane._as_storage
    prod_root = str(prod.root)

    def guarded(store):
        out = real(store)
        if prod_root in str(getattr(out, "root", "")) and not _serving():
            raise AssertionError(f"read in place: {out}")
        return out

    import threading

    def _serving():
        return threading.current_thread() is not threading.main_thread()

    monkeypatch.setattr(dataplane, "_as_storage", guarded)
    project = load_project(make_pond(tmp_path))
    ok_results, _ = hydrate(project, live=True)
    assert [r.status for r in ok_results] == ["ok"]
    ok(run_pond(project))
    assert seen(project)["n"] == 3


def test_as_at_copies_history_and_steps_through_it(tmp_path, prod):
    prod.publish("tx", t(1), events([1]))
    prod.publish("tx", t(2), events([2, 3]))
    prod.publish("tx", t(3), events([4]))
    project = load_project(make_pond(tmp_path))
    results, _ = hydrate(project, as_at=t(1.5))
    assert "copied with its history" in results[0].note

    ok(run_pond(project))
    assert (seen(project)["n"], seen(project)["f"]) == (1, t(1))
    ok(run_pond(project, incremental=True))
    assert (seen(project)["n"], seen(project)["f"]) == (3, t(2))
    assert out(project, "window") == [(2, False)]
    ok(run_pond(project, incremental=True))
    assert (seen(project)["n"], seen(project)["f"]) == (4, t(3))
    with pytest.raises(ValueError, match="no history reaching .* and no @puddle.step"):
        run_pond(project, incremental=True)

    prod.publish("tx", t(4), events([5]))  # a copy doesn't follow production
    ok(run_pond(project))
    assert seen(project)["n"] == 1


def test_as_at_replays_a_definition_through_history(tmp_path, prod):
    for i, hour in enumerate((1, 2, 3)):
        prod.publish("tx", t(hour), events([i * 2 + 1, i * 2 + 2]))
    puddles = """
        from duckstring import puddle

        @puddle("tx.event")
        def odds(p):
            return p.read_source().filter("id % 2 = 1")
    """
    project = load_project(make_pond(tmp_path, puddles))
    hydrate(project, as_at=t(1))
    ok(run_pond(project))
    assert seen(project)["n"] == 1
    ok(run_pond(project, incremental=True, target="most-recent"))
    assert (seen(project)["n"], seen(project)["f"]) == (3, t(3))


def test_as_at_before_a_merge_tables_base_is_refused(tmp_path, prod):
    prod.publish("dim", t(2), prices([(1, 10)]))
    project = load_project(make_pond(tmp_path, sources={"dim": "1.0.0"}, pond="""
        from duckstring import ripple

        @ripple
        def noop(pond):
            pond.write_table("x", pond.con.sql("SELECT 1 AS x"))
    """))
    results, _ = hydrate(project, as_at=t(1))
    assert results[0].status == "failed" and "holds nothing from before" in results[0].error


def test_next_change_waits_for_a_source_that_runs_without_changing(tmp_path, prod):
    """An hourly dimension that changed only at the start reaches each target by its runs, which its change points
    alone wouldn't show: the run is stamped with the oldest Source's freshness."""
    prod.publish("tx", t(1), events([1]))
    prod.publish("tx", t(3), events([2]))
    prod.publish("dim", t(1), prices([(1, 10)]))
    project = load_project(make_pond(tmp_path, sources={"tx": "1.0.0", "dim": "1.0.0"}))
    hydrate(project, as_at=t(1))
    state = json.loads((project.puddles_dir / "hydrated.json").read_text())
    state["sources"]["dim"]["runs"] = [t(2).isoformat(), t(3.5).isoformat()]  # ran, didn't change
    state["sources"]["tx"]["runs"] = [t(3).isoformat()]
    (project.puddles_dir / "hydrated.json").write_text(json.dumps(state))

    ok(run_pond(project))
    ok(run_pond(project, incremental=True))  # next change: tx at 3; dim reaches it at 3.5
    assert (seen(project)["n"], seen(project)["f"]) == (2, t(3))
    position = json.loads((project.puddles_dir / "position.json").read_text())
    assert position["sources"] == {"tx": t(3).isoformat(), "dim": t(3.5).isoformat()}


def test_to_and_next_run(tmp_path, prod):
    for hour in (1, 2, 3):
        prod.publish("tx", t(hour), events([hour]))
    project = load_project(make_pond(tmp_path))
    hydrate(project, as_at=t(1))
    ok(run_pond(project))
    ok(run_pond(project, incremental=True, to=t(2.5)))  # reached at 3: read as it stood then
    assert (seen(project)["n"], seen(project)["f"]) == (3, t(3))
    with pytest.raises(ValueError, match="isn't after"):
        run_pond(project, incremental=True, to=t(2))
    with pytest.raises(ValueError, match="--next-run needs"):
        run_pond(project, incremental=True, target="next-run")


# ── steps ────────────────────────────────────────────────────────────────────

_STEPPED = """
    from duckstring import puddle

    @puddle("tx.event")
    def base(p):
        p.append_table(p.con.sql("SELECT range AS id, range * 10 AS value FROM range(2)"), pk="id")

    @puddle.step("tx.event")
    def more(p):
        assert p.f > p.previous_f
        p.append_table(p.con.sql(f"SELECT {p.step} * 100 + range AS id, 1 AS value FROM range(3)"), pk="id")
"""


def test_steps_generate_windows_stamped_now_and_reset_with_the_baseline(tmp_path):
    project = load_project(make_pond(tmp_path, _STEPPED))
    hydrate(project)
    ok(run_pond(project))
    base = seen(project)
    assert base["n"] == 2

    first = ok(run_pond(project, incremental=True))
    assert first.stepped == ["tx"] and seen(project)["n"] == 5 and first.f > base["f"]
    assert out(project, "window") == [(3, False)]
    ok(run_pond(project, incremental=True))
    assert seen(project)["n"] == 8
    assert json.loads((project.puddles_dir / "position.json").read_text())["step"] == 2

    ok(run_pond(project))  # the baseline drops what the steps wrote
    assert seen(project)["n"] == 2
    ok(run_pond(project, incremental=True))
    assert seen(project)["n"] == 5  # step 1 again: the same rows


def test_a_step_goes_on_past_copied_history_stamped_at_the_target(tmp_path, prod):
    """Mixed: tx replays real history while dim, a generated Puddle, steps beside it at the same freshness."""
    prod.publish("tx", t(1), events([1]))
    prod.publish("tx", t(2), events([2]))
    puddles = """
        from duckstring import puddle

        @puddle("dim.price")
        def price(p):
            p.merge_table(p.con.sql("SELECT 1 AS k, 10 AS p"), pk="k")

        @puddle.step("dim.price")
        def reprice(p):
            p.merge_table(p.con.sql(f"SELECT 1 AS k, {10 + p.step} AS p"), pk="k")
    """
    project = load_project(make_pond(tmp_path, puddles, sources={"tx": "1.0.0", "dim": "1.0.0"}))
    hydrate(project, as_at=t(1))
    ok(run_pond(project))
    result = ok(run_pond(project, incremental=True))
    assert result.stepped == ["dim"] and result.f == t(2)
    assert seen(project)["n"] == 2


# ── incremental logic gives the baseline's answer ────────────────────────────


def test_an_incremental_aggregate_replayed_matches_a_baseline_at_the_end(tmp_path, prod):
    pond = """
        from duckstring import agg, ripple

        @ripple
        def totals(pond):
            pond.trickle("tx.event").mutate(k="id % 3").aggregate(by="k", n=agg.count(), s=agg.sum("value")).merge(
                "totals", pk="k")
    """
    prod.publish("tx", t(1), events(range(0, 6)))
    prod.publish("tx", t(2), events(range(6, 9)))
    prod.publish("tx", t(3), events(range(9, 15)))
    project = load_project(make_pond(tmp_path, pond=pond))
    hydrate(project, as_at=t(1))
    ok(run_pond(project))
    ok(run_pond(project, incremental=True))
    ok(run_pond(project, incremental=True))
    stepped = sorted(out(project, "totals"))

    hydrate(project, as_at=t(3))
    ok(run_pond(project))
    assert sorted(out(project, "totals")) == stepped


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_hydrate_live_run_incremental_and_ls(runner, tmp_path, prod, monkeypatch):
    prod.publish("tx", t(1), events([1]))
    monkeypatch.chdir(make_pond(tmp_path))
    result = runner.invoke(app, ["pond", "hydrate", "--live"])
    assert result.exit_code == 0, result.output
    assert "live" in result.output
    result = runner.invoke(app, ["pond", "run"])
    assert result.exit_code == 0, result.output
    prod.publish("tx", t(2), events([2]))
    result = runner.invoke(app, ["pond", "run", "--incremental", "--next-change"])
    assert result.exit_code == 0, result.output
    assert t(2).isoformat() in result.output
    result = runner.invoke(app, ["pond", "puddle", "ls"])
    assert result.exit_code == 0, result.output
    assert "live" in result.output and "counter.seen" in result.output
    result = runner.invoke(app, ["pond", "run", "--next-run"])
    assert result.exit_code == 1 and "goes with --incremental" in result.output


def test_a_locally_served_app_reads_live_puddles(tmp_path, prod, monkeypatch):
    """``pond run`` for an App hands the App's handle where each live Source is, so it reads it in place."""
    prod.publish("tx", t(1), events([1, 2]))
    make_pond(tmp_path)
    (tmp_path / "pond.toml").write_text('[pond]\nname = "viewer"\nversion = "0.1.0"\ntype = "app"\n\n'
                                        '[app]\ncommand = "true"\n\n[sources]\ntx = "1.0.0"\n')
    (tmp_path / "src" / "pond.py").unlink()
    project = load_project(tmp_path)
    hydrate(project, live=True)

    from duckstring import app as app_module
    from duckstring.local.app import app_env

    monkeypatch.setenv(app_module.ENV, app_env(project, 8123)[app_module.ENV])
    monkeypatch.setattr(app_module, "_handle", None)
    pond = app_module.pond()
    pond.read_table("tx.event")
    assert pond.con.sql("SELECT count(*) FROM event").fetchall() == [(2,)]
