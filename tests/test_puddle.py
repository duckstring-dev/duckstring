"""Puddles + the local runner: `pond hydrate`, `pond run`, `puddle ls/show/query`,
and the pond.toml ripples/puddles entrypoint declarations."""

from __future__ import annotations

import textwrap
from pathlib import Path

import duckdb
import httpx
import pytest

from duckstring.cli import app
from duckstring.local import hydrate, load_project, run_pond

pytestmark = pytest.mark.timeout(15)  # hydrate/run tests do real DuckDB + parquet I/O

_DEMO_DIR = Path(__file__).parent.parent / "src" / "duckstring" / "demo"


def _make_pond(
    path: Path,
    name: str = "salesish",
    sources: dict[str, str] | None = None,
    pond_py: str | None = None,
    puddles_py: str | None = None,
    toml_extra: str = "",
) -> Path:
    sources = {"tx": "1.0.0"} if sources is None else sources
    toml = f'[pond]\nname = "{name}"\nversion = "0.1.0"\n{toml_extra}'
    if sources:
        toml += "\n[sources]\n" + "".join(f'{s} = "{v}"\n' for s, v in sources.items())
    (path / "pond.toml").write_text(toml)
    (path / "src").mkdir(exist_ok=True)
    if pond_py is None:
        pond_py = """
            from duckstring import ripple

            @ripple
            def shape(pond):
                pond.read_table("tx.event")  # registers the Source table as the view `event`
                pond.write_table("shaped", pond.con.sql("SELECT id, value * 2 AS doubled FROM event"))

            @ripple(parents=[shape])
            def total(pond):
                pond.write_table("total", pond.con.sql("SELECT sum(doubled) AS grand FROM shaped"))
        """
    (path / "src" / "pond.py").write_text(textwrap.dedent(pond_py))
    if puddles_py is None:
        puddles_py = """
            from duckstring import puddle

            @puddle("tx.event")
            def tx_event(p):
                return p.con.sql("SELECT range AS id, range * 10 AS value FROM range(5)")
        """
    (path / "src" / "puddles.py").write_text(textwrap.dedent(puddles_py))
    return path


# ── hydrate ──────────────────────────────────────────────────────────────────


def test_hydrate_synthetic_materialises_snapshot(tmp_path):
    project = load_project(_make_pond(tmp_path))
    results, warnings = hydrate(project)
    assert [r.status for r in results] == ["ok"]
    assert warnings == []
    pq = tmp_path / "puddles" / "ponds" / "tx" / "data" / "event.parquet"
    assert pq.exists()
    (count,) = duckdb.sql(f"SELECT count(*) FROM read_parquet('{pq}')").fetchone()
    assert count == 5


def test_hydrate_path_puddle_copies_file(tmp_path):
    sample = tmp_path / "sample.parquet"
    duckdb.sql("SELECT 1 AS id, 100 AS value").write_parquet(str(sample))
    puddles_py = f"""
        from duckstring import puddle

        @puddle("tx.event")
        def tx_event(p):
            return r"{sample}"
    """
    project = load_project(_make_pond(tmp_path, puddles_py=puddles_py))
    results, _ = hydrate(project)
    assert results[0].status == "ok"
    pq = tmp_path / "puddles" / "ponds" / "tx" / "data" / "event.parquet"
    rows = duckdb.sql(f"SELECT * FROM read_parquet('{pq}')").fetchall()
    assert rows == [(1, 100)]


def test_hydrate_whole_source_puddle_names_tables(tmp_path):
    puddles_py = """
        from duckstring import puddle

        @puddle("tx")
        def tx(p):
            p.write_table("event", p.con.sql("SELECT 1 AS id, 10 AS value"))
            p.write_table("meta", p.con.sql("SELECT 'a' AS k"))
    """
    project = load_project(_make_pond(tmp_path, puddles_py=puddles_py))
    results, _ = hydrate(project)
    assert results[0].status == "ok"
    data = tmp_path / "puddles" / "ponds" / "tx" / "data"
    assert {f.name for f in data.glob("*.parquet")} == {"event.parquet", "meta.parquet"}


def test_hydrate_missing_definition_warns_and_skips(tmp_path):
    project = load_project(_make_pond(tmp_path, sources={"tx": "1.0.0", "other": "1.0.0"}))
    results, warnings = hydrate(project)
    assert [r.status for r in results] == ["ok"]
    assert any("other" in w and "skipped" in w for w in warnings)
    assert not (tmp_path / "puddles" / "ponds" / "other").exists()


def test_hydrate_undeclared_target_errors(tmp_path):
    puddles_py = """
        from duckstring import puddle

        @puddle("nope.event")
        def nope(p):
            return p.con.sql("SELECT 1")
    """
    project = load_project(_make_pond(tmp_path, puddles_py=puddles_py))
    with pytest.raises(ValueError, match="nope.event"):
        hydrate(project)


def test_hydrate_failure_is_reported_not_raised(tmp_path):
    puddles_py = """
        from duckstring import puddle

        @puddle("tx.event")
        def tx_event(p):
            raise RuntimeError("boom")
    """
    project = load_project(_make_pond(tmp_path, puddles_py=puddles_py))
    results, _ = hydrate(project)
    assert results[0].status == "failed"
    assert "boom" in results[0].error
    assert "RuntimeError" in results[0].traceback


def test_hydrate_cli(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(_make_pond(tmp_path))
    result = runner.invoke(app, ["pond", "hydrate"])
    assert result.exit_code == 0, result.output
    assert "tx.event" in result.output


# ── pond run ─────────────────────────────────────────────────────────────────


def test_run_full_pond_exports_output(tmp_path):
    project = load_project(_make_pond(tmp_path))
    hydrate(project)
    result = run_pond(project)
    assert result.ok
    assert [r.name for r in result.ripples] == ["shape", "total"]
    out = tmp_path / "puddles" / "out"
    rows = duckdb.sql(f"SELECT grand FROM read_parquet('{out / 'total.parquet'}')").fetchall()
    assert rows == [((0 + 10 + 20 + 30 + 40) * 2,)]  # sum(value) doubled
    assert (out / "shaped.parquet").exists()


def test_run_previous_f_never_then_prior_run(tmp_path):
    """pond.previous_f mirrors a deployed Pond: NEVER on a fresh first run, and the prior local run's
    freshness once a self-puddle seed makes the rerun incremental. --fresh resets it to NEVER."""
    import shutil

    pond_py = """
        from duckstring import ripple

        @ripple
        def echo(pond):
            pond.write_table("meta", pond.con.sql(
                f"SELECT '{pond.previous_f.isoformat()}' AS prev, '{pond.f.isoformat()}' AS cur"
            ))
    """
    project = load_project(_make_pond(
        tmp_path, name="echoer", sources={}, pond_py=pond_py,
        puddles_py="from duckstring import puddle\n",
    ))
    out = tmp_path / "puddles" / "out" / "meta.parquet"

    run_pond(project)  # first run: no prior → NEVER
    prev1, cur1 = duckdb.sql(f"SELECT prev, cur FROM read_parquet('{out}')").fetchone()
    assert prev1.startswith("0001-01-01")  # NEVER

    # Seed a self-puddle so the next run is an incremental rerun (seeded=True).
    selfdir = tmp_path / "puddles" / "ponds" / "echoer" / "data"
    selfdir.mkdir(parents=True)
    shutil.copy(out, selfdir / "meta.parquet")

    run_pond(project)  # second run: seeded → previous_f is the prior run's f
    prev2, _ = duckdb.sql(f"SELECT prev, cur FROM read_parquet('{out}')").fetchone()
    assert prev2 == cur1

    run_pond(project, fresh=True)  # --fresh: not seeded → back to NEVER
    prev3, _ = duckdb.sql(f"SELECT prev, cur FROM read_parquet('{out}')").fetchone()
    assert prev3.startswith("0001-01-01")


def test_run_single_ripple_uses_existing_state(tmp_path):
    project = load_project(_make_pond(tmp_path))
    hydrate(project)
    run_pond(project)
    result = run_pond(project, ripple="total")
    assert result.ok
    assert [r.name for r in result.ripples] == ["total"]


def test_run_unknown_ripple_errors(tmp_path):
    project = load_project(_make_pond(tmp_path))
    with pytest.raises(ValueError, match="no ripple"):
        run_pond(project, ripple="nope")


def test_run_ripple_failure_stops_and_carries_traceback(tmp_path):
    pond_py = """
        from duckstring import ripple

        @ripple
        def bad(pond):
            raise RuntimeError("ripple boom")

        @ripple(parents=[bad])
        def never(pond):
            pond.write_table("never", pond.con.sql("SELECT 1"))
    """
    project = load_project(_make_pond(tmp_path, pond_py=pond_py))
    result = run_pond(project)
    assert not result.ok
    assert [r.name for r in result.ripples] == ["bad"]  # stopped at the failure
    assert "ripple boom" in result.ripples[0].error
    assert "RuntimeError" in result.ripples[0].traceback


def test_run_overwrite_is_deterministic(tmp_path):
    project = load_project(_make_pond(tmp_path))
    hydrate(project)
    run_pond(project)
    out = tmp_path / "puddles" / "out" / "total.parquet"
    first = duckdb.sql(f"SELECT * FROM read_parquet('{out}')").fetchall()
    run_pond(project)
    assert duckdb.sql(f"SELECT * FROM read_parquet('{out}')").fetchall() == first


# ── incremental: self-puddle seed ────────────────────────────────────────────

_INCREMENTAL_POND = """
    from duckstring import ripple

    @ripple
    def grow(pond):
        new = pond.con.sql("SELECT 99 AS id, 990 AS value")  # noqa: F841
        try:
            existing = pond.read_table("events")  # noqa: F841
            combined = pond.con.sql("SELECT * FROM existing UNION ALL SELECT * FROM new")
        except Exception:
            combined = new
        pond.write_table("events", combined)
"""

_SELF_PUDDLE = """
    from duckstring import puddle

    @puddle("growth.events")
    def prior(p):
        return p.con.sql("SELECT range AS id, range AS value FROM range(3)")
"""


def test_incremental_seed_makes_reruns_idempotent(tmp_path):
    project = load_project(
        _make_pond(tmp_path, name="growth", sources={}, pond_py=_INCREMENTAL_POND, puddles_py=_SELF_PUDDLE)
    )
    hydrate(project)
    out = tmp_path / "puddles" / "out" / "events.parquet"

    result = run_pond(project)
    assert result.seeded
    first = sorted(duckdb.sql(f"SELECT * FROM read_parquet('{out}')").fetchall())
    assert len(first) == 4  # 3 seeded + 1 appended

    run_pond(project)  # re-seeded from the same puddle → identical result
    assert sorted(duckdb.sql(f"SELECT * FROM read_parquet('{out}')").fetchall()) == first


def test_fresh_skips_the_seed(tmp_path):
    project = load_project(
        _make_pond(tmp_path, name="growth", sources={}, pond_py=_INCREMENTAL_POND, puddles_py=_SELF_PUDDLE)
    )
    hydrate(project)
    result = run_pond(project, fresh=True)
    assert not result.seeded
    out = tmp_path / "puddles" / "out" / "events.parquet"
    assert len(duckdb.sql(f"SELECT * FROM read_parquet('{out}')").fetchall()) == 1


# ── puddle ls / show / query ─────────────────────────────────────────────────


def test_puddle_ls_show_query(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(_make_pond(tmp_path))
    project = load_project(tmp_path)
    hydrate(project)
    run_pond(project)

    result = runner.invoke(app, ["puddle", "ls"])
    assert result.exit_code == 0, result.output
    assert "tx.event" in result.output
    assert "salesish.total" in result.output

    result = runner.invoke(app, ["puddle", "show", "tx.event"])
    assert result.exit_code == 0, result.output
    assert "value" in result.output

    result = runner.invoke(app, ["puddle", "query", 'SELECT grand FROM "salesish"."total"'])
    assert result.exit_code == 0, result.output
    assert "200" in result.output


def test_puddle_ls_empty(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(_make_pond(tmp_path))
    result = runner.invoke(app, ["puddle", "ls"])
    assert result.exit_code == 0, result.output
    assert "hydrate" in result.output


# ── demo sales pond runs locally end-to-end ──────────────────────────────────


@pytest.mark.timeout(30)
def test_demo_sales_hydrates_and_runs(tmp_path):
    import shutil

    shutil.copytree(_DEMO_DIR / "sales", tmp_path / "sales")
    project = load_project(tmp_path / "sales")
    results, warnings = hydrate(project)
    assert {r.target for r in results} == {"transactions.transaction", "products.product"}
    assert all(r.status == "ok" for r in results)
    assert warnings == []

    result = run_pond(project)
    assert result.ok, [r.error for r in result.ripples]
    out = tmp_path / "sales" / "puddles" / "out" / "sale_line.parquet"
    (count,) = duckdb.sql(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()
    assert count > 0


# ── pond.toml entrypoint declarations ────────────────────────────────────────


def test_custom_entrypoints_run_locally(tmp_path):
    _make_pond(tmp_path, toml_extra='ripples = "transforms/main.py"\npuddles = "transforms/snapshots.py"\n')
    transforms = tmp_path / "transforms"
    transforms.mkdir()
    (tmp_path / "src" / "pond.py").rename(transforms / "main.py")
    (tmp_path / "src" / "puddles.py").rename(transforms / "snapshots.py")

    project = load_project(tmp_path)
    results, _ = hydrate(project)
    assert [r.status for r in results] == ["ok"]
    result = run_pond(project)
    assert result.ok
    assert (tmp_path / "puddles" / "out" / "total.parquet").exists()


@pytest.mark.timeout(30)
def test_custom_ripples_entrypoint_deploys(tmp_path, catchment_client):
    from duckstring.cli.deploy import _zip_pond

    _make_pond(tmp_path, name="custom_entry", toml_extra='ripples = "transforms/main.py"\n')
    transforms = tmp_path / "transforms"
    transforms.mkdir()
    (tmp_path / "src" / "pond.py").rename(transforms / "main.py")

    archive = _zip_pond(tmp_path)
    resp = catchment_client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", archive, "application/zip")},
        data={"name": "custom_entry", "version": "0.1.0"},
    )
    assert resp.status_code == 200, resp.text
    status = catchment_client.get("/api/status").json()
    pond = next(p for p in status["ponds"] if p["name"] == "custom_entry")
    assert {r["name"] for r in pond["ripples"]} == {"shape", "total"}


# ── pulling puddles from a Catchment ─────────────────────────────────────────


@pytest.mark.timeout(30)
def test_catchment_get_puddle(tmp_path, live_catchment, catchment_root):
    data_dir = catchment_root / "ponds" / "tx" / "m1" / "data"
    data_dir.mkdir(parents=True)
    duckdb.sql("SELECT 7 AS id, 70 AS value").write_parquet(str(data_dir / "event.parquet"))

    puddles_py = """
        from duckstring import puddle

        @puddle("tx.event")
        def tx_event(p):
            return p.catchment().get()
    """
    project = load_project(_make_pond(tmp_path, puddles_py=puddles_py))
    results, _ = hydrate(project)
    assert results[0].status == "ok", results[0].error
    pq = tmp_path / "puddles" / "ponds" / "tx" / "data" / "event.parquet"
    assert duckdb.sql(f"SELECT * FROM read_parquet('{pq}')").fetchall() == [(7, 70)]


@pytest.mark.timeout(30)
def test_from_catchment_fills_missing_sources(tmp_path, live_catchment, catchment_root):
    data_dir = catchment_root / "ponds" / "other" / "m1" / "data"
    data_dir.mkdir(parents=True)
    duckdb.sql("SELECT 1 AS a").write_parquet(str(data_dir / "t1.parquet"))
    duckdb.sql("SELECT 2 AS b").write_parquet(str(data_dir / "t2.parquet"))

    project = load_project(_make_pond(tmp_path, sources={"tx": "1.0.0", "other": "1.0.0"}))
    results, warnings = hydrate(project, from_catchment=True)
    assert {r.target: r.status for r in results} == {"tx.event": "ok", "other": "ok"}
    assert any("pulling from the Catchment" in w for w in warnings)
    data = tmp_path / "puddles" / "ponds" / "other" / "data"
    assert {f.name for f in data.glob("*.parquet")} == {"t1.parquet", "t2.parquet"}
    # sanity: httpx reached the live server, not a mock
    assert httpx.get(f"{live_catchment}/api/health", timeout=2.0).status_code == 200


def test_local_run_exposes_run_freshness(tmp_path, monkeypatch):
    """A local run stamps one freshness for the whole run — pond.f is set and tz-aware."""
    monkeypatch.chdir(_make_pond(tmp_path, sources={}, pond_py="""
        from duckstring import ripple

        @ripple
        def stamp(pond):
            assert pond.f is not None and pond.f.tzinfo is not None
            pond.write_table("stamped", pond.con.sql(f"SELECT '{pond.f.isoformat()}' AS run_f"))
    """))
    from duckstring.local import load_project, run_pond

    result = run_pond(load_project())
    assert result.ok, [r.error for r in result.ripples]
