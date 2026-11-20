"""Puddle Sources on a dev Catchment (plans/showcase-fixes.md item 10, catchment/puddles.py): a Pond deployed to a
Catchment named in ``puddles.toml`` reads the Sources listed there from production, through production's query
API, at positions the dev Catchment moves through production's history. Real Ducks; production is a Catchment root
a test publishes into (as in ``test_live_puddles``), served over HTTP and registered as ``prod``."""

from __future__ import annotations

import time
from datetime import datetime

import duckdb
import httpx
import pytest
from test_live_puddles import Prod, events, make_pond, seen, t

from duckstring.cli import app
from duckstring.local import hydrate, load_project, run_pond

pytestmark = pytest.mark.timeout(120)

_DEV_PUDDLES = """
    [dev]
    tx = "default"

    [local]
    tx = "default"
"""


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


def _project(tmp_path, *args, **kwargs):
    """The Pond under test, beside the Catchment roots rather than around them (a deploy uploads its directory)."""
    path = tmp_path / "counter"
    path.mkdir(exist_ok=True)
    return make_pond(path, *args, **kwargs)


def _status(url: str, name: str) -> dict | None:
    return next((p for p in httpx.get(f"{url}/api/status", timeout=5.0).json()["ponds"] if p["name"] == name), None)


def _published(root, table: str) -> list[tuple]:
    from duckstring.dataplane import ParquetDataPlane

    select = ParquetDataPlane().read_select(root / "ponds" / "counter" / "m0" / "data", table)  # version 0.1.0
    con = duckdb.connect()
    cols = [c for c in con.sql(select).columns if not c.startswith("_duckstring_")]
    return con.sql(f"SELECT {', '.join(cols)} FROM ({select})").fetchall()


def _dev_seen(root) -> dict:
    n, total, f, prev = _published(root, "seen")[0]
    return {"n": n, "total": total, "f": datetime.fromisoformat(f), "prev": prev}


def _settled(url: str, at: datetime, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = _status(url, "counter") or {}
        if st.get("end_f") and datetime.fromisoformat(st["end_f"]) >= at and st["status"] == "idle":
            return st
        if st.get("is_failed"):
            pytest.fail(f"counter failed: {st.get('error')}")
        time.sleep(0.2)
    pytest.fail(f"counter never reached {at.isoformat()}: {_status(url, 'counter')}")


def _cli(runner, *args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_a_dev_catchment_steps_through_production_as_a_local_run_does(runner, tmp_path, catchments, monkeypatch):
    prod, dev, dev_root = catchments
    prod.publish("tx", t(0), events([1, 2]))
    prod.publish("tx", t(1), events([3]))
    prod.publish("tx", t(2), events([4, 5]))
    project_dir = _project(tmp_path, toml=_DEV_PUDDLES)
    monkeypatch.chdir(project_dir)

    output = _cli(runner, "pond", "deploy", "-y")
    assert "Reads as Puddles here: tx (default)" in output

    # The Puddle node stands in for tx, which isn't deployed here.
    node = _status(dev, "tx~puddle")
    assert node is not None and node["is_puddle"] and node["is_draw"]
    assert _status(dev, "counter")["puddle_sources"] == {"tx": "default"}

    # A Refresh at now can't be met by production's history: refused, naming the way forward.
    r = httpx.post(f"{dev}/api/ponds/counter/refresh", timeout=5.0)
    assert r.status_code == 422 and "--next-change" in r.json()["detail"]

    # The baseline: tx as of t(0.5), so its t(0) publish.
    _cli(runner, "refresh", "now", "counter", "--hydrate", "--live", "--from", "prod",
         "--as-at", t(0.5).isoformat(), "--wait")
    _settled(dev, t(0))
    assert _dev_seen(dev_root)["n"] == 2
    assert _published(dev_root, "window") == [(2, True)]
    assert _status(dev, "tx~puddle")["puddle"]["position_f"] == t(0).isoformat()

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


def test_a_puddle_definition_runs_on_production(runner, tmp_path, catchments, monkeypatch):
    """The Duck compiles the Pond's definition and its query runs on production: only even ids arrive."""
    prod, dev, dev_root = catchments
    prod.publish("tx", t(0), events([1, 2, 3, 4]))
    prod.publish("tx", t(1), events([6, 7]))
    puddles = """
        from duckstring import puddle

        @puddle("tx.event", set="evens")
        def evens(p):
            return p.read_source().filter("id % 2 = 0")
    """
    project_dir = _project(tmp_path, puddles, toml='[dev]\ntx = "evens"\n')
    monkeypatch.chdir(project_dir)
    _cli(runner, "pond", "deploy", "-y")
    _cli(runner, "refresh", "now", "counter", "--hydrate", "--live", "--from", "prod",
         "--as-at", t(0.5).isoformat(), "--next-change", "--wait")
    _settled(dev, t(1))
    assert _dev_seen(dev_root)["n"] == 3  # 2, 4, then 6
    assert _published(dev_root, "window") == [(1, False)]


def test_puddles_toml_is_checked_and_push_changes_it_without_a_redeploy(runner, tmp_path, catchments, monkeypatch):
    _prod, dev, _ = catchments
    project_dir = _project(tmp_path, toml='[dev]\nnope = "default"\n')
    monkeypatch.chdir(project_dir)
    result = runner.invoke(app, ["pond", "deploy", "-y"])
    assert result.exit_code != 0 and "doesn't read" in result.output

    (project_dir / "puddles.toml").write_text('[elsewhere]\ntx = "default"\n')
    output = _cli(runner, "pond", "deploy", "-y")
    assert "Puddles" not in output
    assert _status(dev, "tx~puddle") is None  # no line reads tx as a Puddle here
    assert _status(dev, "counter")["missing_sources"] == ["tx@1"]

    (project_dir / "puddles.toml").write_text('[dev]\ntx = "unknown"\n')
    result = runner.invoke(app, ["pond", "puddle", "push"])
    assert result.exit_code != 0 and "no Puddle is in that set" in result.output

    (project_dir / "puddles.toml").write_text('[dev]\ntx = "default"\n')
    assert "tx (default)" in _cli(runner, "pond", "puddle", "push")
    assert _status(dev, "tx~puddle") is not None
    assert _status(dev, "counter")["missing_sources"] == []

    (project_dir / "puddles.toml").unlink()
    _cli(runner, "pond", "puddle", "push")
    assert _status(dev, "tx~puddle") is None


def test_a_target_before_any_hydration_is_refused(runner, tmp_path, catchments, monkeypatch):
    _prod, dev, _ = catchments
    monkeypatch.chdir(_project(tmp_path, toml='[dev]\ntx = "default"\n'))
    _cli(runner, "pond", "deploy", "-y")
    r = httpx.post(f"{dev}/api/ponds/counter/puddles/refresh", json={"target": "next-change"}, timeout=5.0)
    assert r.status_code == 422 and "--hydrate" in r.json()["detail"]
    r = httpx.post(f"{dev}/api/ponds/counter/puddles/rewind", timeout=5.0)
    assert r.status_code == 422 and "--hydrate" in r.json()["detail"]


def test_a_duck_reads_only_its_own_puddle_sources(runner, tmp_path, catchments, monkeypatch):
    """The Duck channel forwards a query only for a Puddle Source of the asking line."""
    _prod, dev, dev_root = catchments
    monkeypatch.chdir(_project(tmp_path, toml='[dev]\ntx = "default"\n'))
    _cli(runner, "pond", "deploy", "-y")
    from duckstring.catchment.db import connect

    con = connect(dev_root / "duck.db")
    token = con.execute("SELECT value FROM catchment_meta WHERE key = 'duck_token'").fetchall()
    con.close()
    headers = {"X-Duck-Token": token[0][0]} if token else {}
    r = httpx.post(f"{dev}/api/duck/counter/0/puddle/query", headers=headers, timeout=5.0,
                   json={"pond": "other", "major": 1, "sql": "SELECT 1", "format": "parquet"})
    assert r.status_code == 403
    r = httpx.post(f"{dev}/api/duck/counter/0/puddle/get", headers=headers, timeout=5.0,
                   json={"path": "/api/catchment/archive", "params": {}})
    assert r.status_code == 403


# ── the Puddle node, without Ducks ──────────────────────────────────────────


def _zip(path) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in path.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


def test_a_puddle_position_survives_a_restart_and_goes_with_its_last_reader(tmp_path):
    import json

    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app

    project = _project(tmp_path, toml='[dev]\ntx = "default"\n')
    root = tmp_path / "dev"
    root.mkdir()
    with TestClient(create_app(root, name="dev")) as client:
        r = client.post("/api/deploy", files={"pond": ("pond.zip", _zip(project), "application/zip")},
                        data={"name": "counter", "version": "0.1.0",
                              "puddles": json.dumps({"dev": {"tx": "default"}})})
        assert r.status_code == 200, r.text
        assert r.json()["puddle_sources"] == [{"source": "tx", "set": "default"}]
        driver = client.app.state.driver
        assert driver.state.ponds["counter@0"].sources == ["tx~puddle@1"]
        with driver.lock:
            driver._move_puddle("tx~puddle@1", t(3))
        # A recurring Refresh reaching a Puddle would wait for good: refused.
        r = client.post("/api/ponds/counter/refreshes", json={"every_seconds": 60})
        assert r.status_code == 422 and "Puddles" in r.json()["detail"]
    with TestClient(create_app(root, name="dev")) as client:
        driver = client.app.state.driver
        assert driver.state.pond_states["tx~puddle@1"].remote_f == t(3)
        assert client.delete("/api/ponds/counter").status_code == 200
        assert "tx~puddle@1" not in driver.state.ponds
    # A Catchment of another name ignores the section: the line reads the Source it declares.
    with TestClient(create_app(tmp_path / "other", name="other")) as client:
        r = client.post("/api/deploy", files={"pond": ("pond.zip", _zip(project), "application/zip")},
                        data={"name": "counter", "version": "0.1.0",
                              "puddles": json.dumps({"dev": {"tx": "default"}})})
        assert r.status_code == 200 and r.json()["puddle_sources"] == []
        assert client.app.state.driver.meta["counter@0"]["missing_sources"] == ["tx@1"]
