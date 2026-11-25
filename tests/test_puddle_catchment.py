"""Puddles on a dev Catchment (plans/showcase-fixes.md item 10, catchment/puddles.py): a Puddle is a Source line on
that Catchment, deployed from a reading Pond's definitions (``pond puddle deploy``), and every Pond there reads it.
A live Puddle reads production through its query API at the positions the dev Catchment moves it to; a copied one
holds its own data (from production with ``--from``, or from its definitions alone). Real Ducks; production is a
Catchment root a test publishes into (as in ``test_live_puddles``), served over HTTP and registered as ``prod``."""

from __future__ import annotations

import json
import textwrap
import time
from datetime import datetime

import duckdb
import httpx
import pytest
from test_live_puddles import Prod, events, make_pond, seen, t

from duckstring.cli import app
from duckstring.local import hydrate, load_project, run_pond

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def catchments(tmp_path, monkeypatch):
    """``(prod, dev_url, dev_root)``: production served and registered as ``prod``; a Catchment named ``dev`` with
    real Ducks, registered as ``dev`` and the default."""
    from catchment_server import free_port, serve_catchment

    from duckstring.cli.config import register_catchment, set_default_catchment

    prod_root, dev_root = tmp_path / "prod", tmp_path / "dev"
    prod_root.mkdir()
    dev_root.mkdir()
    port = free_port()
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", f"http://127.0.0.1:{port}")
    with serve_catchment(prod_root) as prod_url, serve_catchment(dev_root, name="dev", port=port) as dev_url:
        register_catchment("prod", url=prod_url, kind="local")
        register_catchment("dev", url=dev_url, kind="local")
        set_default_catchment("dev")
        yield Prod(prod_root), dev_url, dev_root


def _project(tmp_path, *args, name: str = "counter", **kwargs):
    """A Pond under test named ``name``, beside the Catchment roots rather than around them (a deploy uploads its
    directory)."""
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    make_pond(path, *args, **kwargs)
    toml = path / "pond.toml"
    toml.write_text(toml.read_text().replace('name = "counter"', f'name = "{name}"'))
    return path


def _status(url: str, name: str) -> dict | None:
    return next((p for p in httpx.get(f"{url}/api/status", timeout=5.0).json()["ponds"] if p["name"] == name), None)


def _published(root, table: str, line: str = "counter/m0") -> list[tuple]:
    from duckstring.dataplane import ParquetDataPlane

    select = ParquetDataPlane().read_select(root / "ponds" / line / "data", table)
    con = duckdb.connect()
    cols = [c for c in con.sql(select).columns if not c.startswith("_duckstring_")]
    return con.sql(f"SELECT {', '.join(cols)} FROM ({select})").fetchall()


def _dev_seen(root) -> dict:
    n, total, f, prev = _published(root, "seen")[0]
    return {"n": n, "total": total, "f": datetime.fromisoformat(f), "prev": prev}


def _settled(url: str, at: datetime | None = None, timeout: float = 60.0, name: str = "counter") -> dict:
    """``name`` idle at ``at`` or later (any Run, without ``at``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = _status(url, name) or {}
        end = datetime.fromisoformat(st["end_f"]) if st.get("end_f") else None
        if end is not None and (at is None or end >= at) and st["status"] == "idle":
            return st
        if st.get("is_failed"):
            pytest.fail(f"{name} failed: {st.get('error')}")
        time.sleep(0.2)
    pytest.fail(f"{name} never reached {at}: {_status(url, name)}")


def _cli(runner, *args, input: str | None = None):
    result = runner.invoke(app, list(args), input=input)
    assert result.exit_code == 0, result.output
    return result.output


def _refused(runner, *args, input: str | None = None) -> str:
    result = runner.invoke(app, list(args), input=input)
    assert result.exit_code != 0, result.output
    return result.output


# ── live Puddles ────────────────────────────────────────────────────────────


def test_a_dev_catchment_steps_through_production_as_a_local_run_does(runner, tmp_path, catchments, monkeypatch):
    prod, dev, dev_root = catchments
    prod.publish("tx", t(0), events([1, 2]))
    prod.publish("tx", t(1), events([3]))
    prod.publish("tx", t(2), events([4, 5]))
    project_dir = _project(tmp_path)
    monkeypatch.chdir(project_dir)

    # The Puddle is the line tx@1 on dev, with its baseline as of t(0.5): tx's t(0) publish.
    output = _cli(runner, "pond", "puddle", "deploy", "--live", "--from", "prod", "--as-at", t(0.5).isoformat())
    assert "tx@1" in output and "live" in output
    node = _status(dev, "tx")
    assert node["is_puddle"] and node["puddle"]["mode"] == "live"
    assert node["puddle"]["baseline_f"] == node["puddle"]["position_f"] == t(0).isoformat()
    _cli(runner, "pond", "deploy", "-y")
    assert _status(dev, "counter")["missing_sources"] == []

    # A Refresh at now can't be met by production's history: refused, naming the way forward.
    r = httpx.post(f"{dev}/api/ponds/counter/refresh", timeout=5.0)
    assert r.status_code == 422 and "--rewind" in r.json()["detail"]

    _cli(runner, "refresh", "now", "counter", "--rewind", "--wait")
    _settled(dev, t(0))
    assert _dev_seen(dev_root)["n"] == 2
    assert _published(dev_root, "window") == [(2, True)]

    # One step: tx's next change, read as an append delta.
    _cli(runner, "refresh", "now", "counter", "--next-change", "--wait")
    _settled(dev, t(1))
    assert _dev_seen(dev_root)["n"] == 3
    assert _published(dev_root, "window") == [(1, False)]

    # Back to the baseline and run from it.
    _cli(runner, "refresh", "now", "counter", "--rewind", "--wait")
    time.sleep(0.5)
    _settled(dev, t(0))
    assert _dev_seen(dev_root)["n"] == 2
    assert _published(dev_root, "window") == [(2, True)]

    # The same steps on one Pond with no Catchment reach the same output.
    project = load_project(project_dir)
    hydrate(project, catchment="prod", live=True, as_at=t(0.5))
    assert run_pond(project).ok
    assert run_pond(project, incremental=True).ok  # next-change
    assert run_pond(project, incremental=True).ok
    local = seen(project)
    _cli(runner, "refresh", "now", "counter", "--next-change", "--wait")
    _settled(dev, t(1))
    _cli(runner, "refresh", "now", "counter", "--most-recent", "--wait")
    _settled(dev, t(2))
    remote = _dev_seen(dev_root)
    assert (remote["n"], remote["total"], remote["f"]) == (local["n"], local["total"], local["f"]) == (5, 150, t(2))

    # A new baseline, with the definitions the Puddle was deployed with: as of now, so tx's newest publish.
    _cli(runner, "refresh", "now", "counter", "--hydrate", "--wait")
    _settled(dev, t(2))
    assert _dev_seen(dev_root)["n"] == 5
    assert _published(dev_root, "window") == [(5, True)]
    assert _status(dev, "tx")["puddle"]["baseline_f"] == t(2).isoformat()


def test_a_puddle_definition_runs_on_production(runner, tmp_path, catchments, monkeypatch):
    """The Puddle's definition was compiled when it was deployed, and its query runs on production: only even ids
    arrive, and the reading Pond has no definitions of its own."""
    prod, dev, dev_root = catchments
    prod.publish("tx", t(0), events([1, 2, 3, 4]))
    prod.publish("tx", t(1), events([6, 7]))
    puddles = """
        from duckstring import puddle

        @puddle("tx.event", set="evens")
        def evens(p):
            return p.read_source().filter("id % 2 = 0")
    """
    monkeypatch.chdir(_project(tmp_path, puddles, name="definer"))
    _cli(runner, "pond", "puddle", "deploy", "--set", "evens", "--live", "--from", "prod",
         "--as-at", t(0.5).isoformat())
    monkeypatch.chdir(_project(tmp_path))
    _cli(runner, "pond", "deploy", "-y")
    _cli(runner, "refresh", "now", "counter", "--rewind", "--next-change", "--wait")
    _settled(dev, t(1))
    assert _dev_seen(dev_root)["n"] == 3  # 2, 4, then 6
    assert _published(dev_root, "window") == [(1, False)]


def test_a_duck_reads_only_its_own_lines_live_puddles(runner, tmp_path, catchments, monkeypatch):
    """The Duck channel forwards a query only for a live Puddle the asking line reads."""
    _prod, dev, dev_root = catchments
    monkeypatch.chdir(_project(tmp_path))
    _cli(runner, "pond", "deploy", "-y")
    from duckstring.catchment.db import connect

    con = connect(dev_root / "duck.db")
    token = con.execute("SELECT value FROM catchment_meta WHERE key = 'duck_token'").fetchall()
    con.close()
    headers = {"X-Duck-Token": token[0][0]} if token else {}
    r = httpx.post(f"{dev}/api/duck/counter/0/puddle/query", headers=headers, timeout=5.0,
                   json={"pond": "tx", "major": 1, "sql": "SELECT 1", "format": "parquet"})
    assert r.status_code == 403
    r = httpx.post(f"{dev}/api/duck/counter/0/puddle/get", headers=headers, timeout=5.0,
                   json={"path": "/api/catchment/archive", "params": {}})
    assert r.status_code == 403


# ── copied Puddles ──────────────────────────────────────────────────────────

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


def test_a_puddle_from_its_definitions_alone_steps_on_the_catchment(runner, tmp_path, catchments, monkeypatch):
    """No --from: dev is wholly synthetic. Its definitions wrote the baseline when it was deployed, and each target
    past it runs its steps, as a local run does."""
    _prod, dev, dev_root = catchments
    monkeypatch.chdir(_project(tmp_path, _STEPPED))
    output = _cli(runner, "pond", "puddle", "deploy")
    assert "tx@1" in output and "copied" in output
    assert (dev_root / "ponds" / "tx" / "m1" / "data" / "_trickle.json").exists()  # its data is dev's own
    _cli(runner, "pond", "deploy", "-y")

    _cli(runner, "refresh", "now", "counter", "--rewind", "--wait")
    base = _settled(dev)
    assert _dev_seen(dev_root)["n"] == 2
    _cli(runner, "refresh", "now", "counter", "--next-change", "--wait")
    time.sleep(0.5)
    first = _settled(dev)
    assert _dev_seen(dev_root)["n"] == 5
    assert _published(dev_root, "window") == [(3, False)]
    assert datetime.fromisoformat(first["end_f"]) > datetime.fromisoformat(base["end_f"])
    _cli(runner, "refresh", "now", "counter", "--next-change", "--wait")
    time.sleep(0.5)
    _settled(dev)
    assert _dev_seen(dev_root)["n"] == 8

    # The baseline drops what the steps wrote, and step 1 writes the same rows again.
    _cli(runner, "refresh", "now", "counter", "--rewind", "--wait")
    time.sleep(0.5)
    _settled(dev)
    assert _dev_seen(dev_root)["n"] == 2
    _cli(runner, "refresh", "now", "counter", "--next-change", "--wait")
    time.sleep(0.5)
    _settled(dev)
    assert _dev_seen(dev_root)["n"] == 5


def test_a_puddle_copied_from_production_steps_through_its_history(runner, tmp_path, catchments, monkeypatch):
    prod, dev, dev_root = catchments
    prod.publish("tx", t(1), events([1]))
    prod.publish("tx", t(2), events([2, 3]))
    prod.publish("tx", t(3), events([4]))
    monkeypatch.chdir(_project(tmp_path))
    output = _cli(runner, "pond", "puddle", "deploy", "--from", "prod", "--as-at", t(1.5).isoformat())
    assert "copied" in output and "history" in output
    _cli(runner, "pond", "deploy", "-y")

    _cli(runner, "refresh", "now", "counter", "--rewind", "--wait")
    _settled(dev, t(1))
    assert _dev_seen(dev_root)["n"] == 1
    _cli(runner, "refresh", "now", "counter", "--next-change", "--wait")
    _settled(dev, t(2))
    assert _dev_seen(dev_root)["n"] == 3
    assert _published(dev_root, "window") == [(2, False)]
    _cli(runner, "refresh", "now", "counter", "--next-change", "--wait")
    _settled(dev, t(3))
    assert _dev_seen(dev_root)["n"] == 4
    assert "no history reaching" in _refused(runner, "refresh", "now", "counter", "--next-change")

    # A copy doesn't follow production: production's next publish changes nothing here.
    prod.publish("tx", t(4), events([5]))
    _cli(runner, "refresh", "now", "counter", "--rewind", "--wait")
    time.sleep(0.5)
    _settled(dev, t(1))
    assert _dev_seen(dev_root)["n"] == 1


# ── what a Catchment holds ──────────────────────────────────────────────────

_DIM_PUDDLE = """
    from duckstring import puddle

    @puddle("dim.price")
    def price(p):
        p.merge_table(p.con.sql("SELECT 1 AS k, 10 AS p"), pk="k")

    @puddle("tx.event")
    def tx(p):
        p.append_table(p.con.sql("SELECT 7 AS id, 70 AS value"), pk="id")
"""


def test_a_source_already_on_the_catchment_is_left_or_replaced_with_confirmation(runner, tmp_path, catchments,
                                                                                 monkeypatch):
    _prod, dev, _ = catchments
    monkeypatch.chdir(_project(tmp_path, _STEPPED))
    _cli(runner, "pond", "puddle", "deploy")
    assert _status(dev, "tx")["puddle"]["owner"] == "counter"

    # Another Pond reading tx and dim deploys only the Source not yet covered.
    monkeypatch.chdir(_project(tmp_path, _DIM_PUDDLE, name="other", sources={"tx": "1.0.0", "dim": "1.0.0"}))
    output = _cli(runner, "pond", "puddle", "deploy")
    assert "dim@1" in output and "Already on dev: tx@1" in output
    assert _status(dev, "tx")["puddle"]["owner"] == "counter"
    assert _status(dev, "dim")["puddle"]["owner"] == "other"

    # Naming it replaces it, once confirmed.
    assert "Not replaced" in _refused(runner, "pond", "puddle", "deploy", "tx", input="n\n")
    assert _status(dev, "tx")["puddle"]["owner"] == "counter"
    _cli(runner, "pond", "puddle", "deploy", "tx", input="y\n")
    assert _status(dev, "tx")["puddle"]["owner"] == "other"
    _cli(runner, "pond", "puddle", "deploy", "tx", "--force")

    # A Pond replaces a Puddle once confirmed; a Puddle never replaces a Pond.
    dim = tmp_path / "dim"
    dim.mkdir()
    (dim / "pond.toml").write_text('[pond]\nname = "dim"\nversion = "1.0.0"\n')
    (dim / "src").mkdir()
    (dim / "src" / "pond.py").write_text(textwrap.dedent("""
        from duckstring import ripple

        @ripple
        def price(pond):
            pond.write_table("price", pond.con.sql("SELECT 1 AS k, 10 AS p"))
    """))
    monkeypatch.chdir(dim)
    assert "Puddle" in _refused(runner, "pond", "deploy", input="n\n")
    assert _status(dev, "dim")["is_puddle"]
    _cli(runner, "pond", "deploy", "-y")
    assert not _status(dev, "dim")["is_puddle"]
    monkeypatch.chdir(tmp_path / "other")
    output = _refused(runner, "pond", "puddle", "deploy", "dim", "--force")
    assert "deployed here as a Pond" in output


def test_an_undeclared_source_or_one_with_no_definitions_is_refused(runner, tmp_path, catchments,
                                                                          monkeypatch):
    monkeypatch.chdir(_project(tmp_path, _STEPPED))
    assert "doesn't read" in _refused(runner, "pond", "puddle", "deploy", "nope")
    assert "no Puddle definition for 'counter'" in _refused(runner, "pond", "puddle", "deploy", "counter")
    # A definition that fails is reported, and nothing is deployed.
    (tmp_path / "counter" / "src" / "puddles.py").write_text(textwrap.dedent("""
        from duckstring import puddle

        @puddle("tx.event")
        def broken(p):
            raise RuntimeError("no rows today")
    """))
    assert "no rows today" in _refused(runner, "pond", "puddle", "deploy")
    _, dev, _ = catchments
    assert _status(dev, "tx") is None


# ── the Puddle line, without Ducks ──────────────────────────────────────────


def _zip(path) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in path.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


def _deploy_puddle(client, project, **spec):
    return client.post("/api/puddles", files={"bundle": ("pond.zip", _zip(project), "application/zip")},
                       data={"spec": json.dumps(spec)})


def _deploy(client, project, name="counter", version="0.1.0", **data):
    return client.post("/api/deploy", files={"pond": ("pond.zip", _zip(project), "application/zip")},
                       data={"name": name, "version": version, **data})


def test_a_puddle_position_survives_a_restart_and_goes_when_it_is_removed(tmp_path):
    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app

    project = _project(tmp_path, _DIM_PUDDLE, sources={"tx": "1.0.0", "dim": "1.0.0"})
    root = tmp_path / "dev"
    root.mkdir()
    with TestClient(create_app(root, name="dev")) as client:
        r = _deploy_puddle(client, project)
        assert r.status_code == 200, r.text
        assert sorted(d["source"] for d in r.json()["deployed"]) == ["dim", "tx"]
        assert _deploy(client, project).status_code == 200
        driver = client.app.state.driver
        assert sorted(driver.state.ponds["counter@0"].sources) == ["dim@1", "tx@1"]
        with driver.lock:
            driver._move_puddle("tx@1", t(1000))  # past its baseline (now)
        # A recurring Refresh reaching a Puddle would wait for good: refused.
        r = client.post("/api/ponds/counter/refreshes", json={"every_seconds": 60})
        assert r.status_code == 422 and "Puddle" in r.json()["detail"]
        # Nothing reads tx's definitions after they ran: a Pond reading it can deploy without any.
        assert client.get("/api/puddles").json()["puddles"][0]["source"] in ("dim", "tx")
    with TestClient(create_app(root, name="dev")) as client:
        driver = client.app.state.driver
        assert driver.state.pond_states["tx@1"].remote_f == t(1000)
        assert client.delete("/api/ponds/tx", params={"major": 1}).status_code == 200
        assert "tx@1" not in driver.state.ponds
        assert driver.meta["counter@0"]["missing_sources"] == ["tx@1"]
        assert not (root / "ponds" / "tx" / "m1").exists() and not (root / "puddles" / "tx" / "m1").exists()


def test_deploying_over_a_puddle_or_a_pond(tmp_path):
    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app

    project = _project(tmp_path, _DIM_PUDDLE, sources={"tx": "1.0.0", "dim": "1.0.0"})
    dim = tmp_path / "dim"
    dim.mkdir()
    (dim / "pond.toml").write_text('[pond]\nname = "dim"\nversion = "1.0.0"\n')
    (dim / "src").mkdir()
    (dim / "src" / "pond.py").write_text("from duckstring import ripple\n\n@ripple\ndef a(pond):\n    pass\n")
    root = tmp_path / "dev"
    root.mkdir()
    with TestClient(create_app(root, name="dev")) as client:
        assert _deploy_puddle(client, project, sources=["dim"]).status_code == 200
        r = _deploy_puddle(client, project, sources=["dim"])
        assert r.status_code == 409 and r.json()["detail"]["replace"] == ["dim@1"]
        assert _deploy_puddle(client, project, sources=["dim"], force=True).status_code == 200
        r = _deploy(client, dim, name="dim", version="1.0.0")
        assert r.status_code == 409 and "Puddle" in r.json()["detail"]
        assert _deploy(client, dim, name="dim", version="1.0.0", replace_puddle="true").status_code == 200
        assert not client.app.state.driver.meta["dim@1"].get("is_puddle")
        r = _deploy_puddle(client, project, sources=["dim"], force=True)
        assert r.status_code == 422 and "as a Pond" in r.json()["detail"]
        # Without sources named, a covered one is skipped, never replaced.
        r = _deploy_puddle(client, project)
        assert r.status_code == 200
        assert [d["source"] for d in r.json()["deployed"]] == ["tx"]
        assert r.json()["skipped"] == [{"source": "dim", "major": 1, "as": "pond"}]


def test_a_source_deploys_a_puddle_of_itself(tmp_path):
    """A self-Puddle: the Source's own definitions, targeting its own name, stand in for it on the Catchment, and a
    Pond reading it deploys none of its own. Whichever deploys first wins: a reader's default deploy skips it."""
    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app
    from duckstring.dataplane import ParquetDataPlane

    tx = tmp_path / "tx"
    (tx / "src").mkdir(parents=True)
    (tx / "pond.toml").write_text('[pond]\nname = "tx"\nversion = "1.2.0"\n')
    (tx / "src" / "pond.py").write_text("from duckstring import ripple\n\n@ripple\ndef event(pond):\n    pass\n")
    (tx / "src" / "puddles.py").write_text(textwrap.dedent("""
        from duckstring import puddle

        @puddle("tx.event")
        def event(p):
            p.append_table(p.con.sql("SELECT range AS id, range * 10 AS value FROM range(4)"), pk="id")
    """))
    root = tmp_path / "dev"
    root.mkdir()
    with TestClient(create_app(root, name="dev")) as client:
        r = _deploy_puddle(client, tx)
        assert r.status_code == 200 and r.json()["deployed"] == []  # a Pond's default is its Sources, never itself
        r = _deploy_puddle(client, tx, sources=["tx"])
        assert r.status_code == 200, r.text
        (d,) = r.json()["deployed"]
        assert (d["source"], d["major"], d["owner"], d["mode"]) == ("tx", 1, "tx", "copied")
        select = ParquetDataPlane().read_select(root / "ponds" / "tx" / "m1" / "data", "event")
        assert duckdb.sql(f"SELECT count(*) FROM ({select})").fetchall()[0][0] == 4

        counter = _project(tmp_path, _STEPPED)  # a reader with definitions of its own for tx
        r = _deploy_puddle(client, counter)
        assert r.json()["deployed"] == [] and r.json()["skipped"] == [{"source": "tx", "major": 1, "as": "puddle"}]
        assert client.app.state.driver.puddle_status("tx@1")["owner"] == "tx"
