"""App instances on other machines (plans/app-scaling.md): the agent starts the App where its compute is, requests
and WebSockets reach it through the tunnel, its log comes back, it reads Pond data from the data root, and a stop
or a lost machine ends it. Against a live Catchment; the "machine" is a subprocess with its own empty root."""

from __future__ import annotations

import time

import duckdb
import httpx
import pytest
from app_remote_launcher import FakeRemoteLauncher
from test_app import _SOURCE_PY, _SOURCE_TOML, _zip
from test_runtime import _free_port, _serve

pytestmark = pytest.mark.timeout(120)

_APP_PY = '''import os

from fastapi import FastAPI, WebSocket

from duckstring import app as ds_app

api = FastAPI()
pond = ds_app.pond()
print("remote app booting", flush=True)


@api.get("/healthz")
def healthz():
    return {"ok": True}


@api.get("/where")
def where():
    return {"root": os.environ["DUCKSTRING_APP"], "base": os.environ["DUCKSTRING_APP_PATH"]}


@api.get("/rows")
def rows():
    return {"n": pond.count_table("sales.revenue"), "fresh": str(pond.freshness()["sources"].get("sales"))}


@api.get("/big")
def big():
    return {"blob": "x" * 3_000_000}


@api.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    message = await socket.receive_text()
    await socket.send_text("echo:" + message)
    await socket.close()
'''


def _toml(extra: str = "") -> str:
    return ('[pond]\nname = "dash"\nversion = "1.0.0"\ntype = "app"\nduck = "M"\n\n[sources]\nsales = "1.0.0"\n\n[app]\n'
            'command = "uvicorn main:api --port $PORT --root-path $DUCKSTRING_APP_PATH"\nhealth = "/healthz"\n'
            'state = "none"\n' + extra)


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A live Catchment whose compute is "elsewhere", with a local-path data root the remote machine can read."""
    from duckstring.catchment.registry import pond_data_dir

    data_root = tmp_path / "data-root"
    data_root.mkdir()
    monkeypatch.setenv("DUCKSTRING_DATA_ROOT", str(data_root))
    root = tmp_path / "catchment"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)
    server, thread = _serve(root, port)
    app = server.config.app
    launcher = FakeRemoteLauncher(url, app.state.duck_token, tmp_path / "machines", str(data_root))
    app.state.driver.launcher = launcher
    r = httpx.post(f"{url}/api/deploy", files={"pond": ("pond.zip", _zip({"pond.toml": _SOURCE_TOML,
                                                                            "src/pond.py": _SOURCE_PY}))},
                   data={"name": "sales", "version": "1.0.0", "type": "pond"}, timeout=60)
    assert r.status_code == 200, r.text
    from pathlib import Path

    target = Path(pond_data_dir(root, "sales", 1, str(data_root)).uri(""))  # the durable layer's layout
    target.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(f"COPY (SELECT range AS id FROM range(7)) TO '{target / 'revenue.parquet'}' (FORMAT PARQUET)")
    yield url, root, app, launcher
    server.should_exit = True
    thread.join(timeout=20)
    launcher.shutdown_all()


def _deploy_app(url: str, extra: str = "") -> None:
    r = httpx.post(f"{url}/api/deploy", files={"pond": ("pond.zip", _zip({"pond.toml": _toml(extra),
                                                                            "main.py": _APP_PY}))},
                   data={"name": "dash", "version": "1.0.0", "type": "pond"}, timeout=60)
    assert r.status_code == 200, r.text


def test_an_instance_elsewhere_serves_through_the_tunnel(remote):
    import json

    from websockets.sync.client import connect

    url, root, app, launcher = remote
    _deploy_app(url)
    r = httpx.get(f"{url}/apps/dash/v1/where", timeout=90)
    assert r.status_code == 200, r.text
    where = json.loads(r.json()["root"])
    assert where["root"] != str(root) and "machines" in where["root"]  # it ran on the "other machine"
    assert r.json()["base"] == "/apps/dash/v1"
    assert [k for k, _ in launcher.spawned] == ["dash~1@1"]
    (key, duck), = launcher.spawned
    assert duck["app_instance"] == 1

    assert httpx.get(f"{url}/apps/dash/v1/rows", timeout=30).json()["n"] == 7  # from the data root, directly
    assert len(httpx.get(f"{url}/apps/dash/v1/big", timeout=30).json()["blob"]) == 3_000_000
    with connect(f"ws://127.0.0.1:{url.rsplit(':', 1)[1]}/apps/dash/v1/ws") as ws:
        ws.send("hi")
        assert ws.recv() == "echo:hi"

    rows = httpx.get(f"{url}/api/ponds/dash/app/instances", timeout=10).json()
    assert rows["instances"][0]["id"] == 1 and rows["instances"][0]["ready_at"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        logs = httpx.get(f"{url}/api/ponds/dash/app/logs", timeout=10).text
        if "remote app booting" in logs:
            break
        time.sleep(0.2)
    assert "remote app booting" in logs  # the App's output, forwarded by its agent

    r = httpx.post(f"{url}/api/ponds/dash/app/restart", timeout=30)
    assert r.status_code == 200, r.text
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and launcher.is_running("dash~1@1"):
        time.sleep(0.2)
    assert not launcher.is_running("dash~1@1")  # the old machine was ended
    assert launcher.procs["dash~1@1"].returncode == 0  # by its agent, stopping cleanly


def test_a_lost_machine_ends_its_instance_and_the_next_request_starts_another(remote):
    url, root, app, launcher = remote
    _deploy_app(url, 'idle_timeout = "0"\n')
    assert httpx.get(f"{url}/apps/dash/v1/healthz", timeout=90).status_code == 200
    launcher.kill("dash~1@1")  # the machine vanishes
    supervisor = app.state.app_supervisor
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and supervisor._of("dash@1"):
        time.sleep(0.2)
    assert not supervisor._of("dash@1")
    assert httpx.get(f"{url}/apps/dash/v1/healthz", timeout=90).status_code == 200
    assert [k for k, _ in launcher.spawned] == ["dash~1@1", "dash~2@1"]


def test_an_instance_elsewhere_needs_what_it_can_reach(remote):
    url, root, app, launcher = remote
    r = httpx.post(f"{url}/api/deploy", files={"pond": ("pond.zip", _zip({
        "pond.toml": _toml().replace('state = "none"\n', ''), "main.py": _APP_PY}))},
        data={"name": "dash", "version": "1.0.0", "type": "pond"}, timeout=60)
    assert r.status_code == 200, r.text
    r = httpx.get(f"{url}/apps/dash/v1/healthz", timeout=30)
    assert r.status_code == 503 and 'state = \\"none\\"' in r.text or "state" in r.text
    assert not launcher.spawned
