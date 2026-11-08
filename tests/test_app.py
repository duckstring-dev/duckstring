"""Apps (plans/apps.md): an Outlet that serves HTTP. Deploying one, the engine node, the AppSupervisor's
instances, forwarding (HTTP and WebSockets) with the Catchment's gate, and the read-only Pond handle."""

from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from duckstring.app import app_config

pytestmark = pytest.mark.timeout(90)

_SOURCE_TOML = '[pond]\nname = "sales"\nversion = "1.0.0"\n'
_SOURCE_PY = "from duckstring import ripple\n\n@ripple\ndef revenue(pond):\n    pass\n"

_APP_PY = '''import os

from fastapi import FastAPI, Request, WebSocket

from duckstring import app as ds_app

api = FastAPI()
pond = ds_app.pond()


@api.get("/healthz")
def healthz():
    return {"ok": True}


@api.get("/hello")
def hello(request: Request):
    return {"base": os.environ["DUCKSTRING_APP_PATH"], "state": os.environ.get("DUCKSTRING_APP_STATE"),
            "headers": dict(request.headers), "key_env": os.environ.get("DUCKSTRING_API_KEY")}


@api.get("/rows")
def rows():
    total = pond.read_table("sales.revenue").aggregate("sum(id)").fetchall()[0][0]
    return {"n": pond.count_table("sales.revenue"), "sum": int(total), "fresh": str(pond.freshness()["freshness"])}


@api.get("/mem")
def mem():
    return {"limit": pond.con.execute("SELECT current_setting('memory_limit')").fetchall()[0][0]}


@api.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    message = await socket.receive_text()
    await socket.send_text("echo:" + message)
    await socket.close()
'''


def _app_toml(command: str = "uvicorn main:api --port $PORT --root-path $DUCKSTRING_APP_PATH",
              extra: str = "", sources: str = 'sales = "1.0.0"\n') -> str:
    return ('[pond]\nname = "dash"\nversion = "1.0.0"\ntype = "app"\n\n[sources]\n' + sources +
            f'\n[app]\ncommand = {json.dumps(command)}\nhealth = "/healthz"\n' + extra)


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _deploy(client, name: str, files: dict[str, str], version: str = "1.0.0", headers=None):
    return client.post("/api/deploy", files={"pond": ("pond.zip", _zip(files), "application/zip")},
                       data={"name": name, "version": version, "type": "pond"}, headers=headers or {})


def _seed(root, rows: int = 4) -> None:
    data = root / "ponds" / "sales" / "m1" / "data"
    data.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(f"COPY (SELECT range AS id FROM range({rows})) TO '{data / 'revenue.parquet'}' (FORMAT PARQUET)")


def _deploy_pipeline(client, app_files: dict[str, str] | None = None, headers=None):
    r = _deploy(client, "sales", {"pond.toml": _SOURCE_TOML, "src/pond.py": _SOURCE_PY}, headers=headers)
    assert r.status_code == 200, r.text
    _seed(client.app.state.root)
    r = _deploy(client, "dash", app_files or {"pond.toml": _app_toml(), "main.py": _APP_PY}, headers=headers)
    assert r.status_code == 200, r.text


def _app_entry(client, headers=None) -> dict:
    status = client.get("/api/status", headers=headers or {}).json()
    return next(p for p in status["ponds"] if p["name"] == "dash")


# ─── declaration ───────────────────────────────────────────────────────────────


def test_app_config_validates_the_declaration(tmp_path):
    assert app_config({"pond": {"name": "x"}}) is None
    cfg = app_config({"pond": {"type": "app"}, "app": {"command": "run", "idle_timeout": "1h30m"}})
    assert cfg == {"command": "run", "static": None, "memory": None, "idle_timeout_s": 5400, "state": "local",
                   "health": None, "strip_path": True, "download": None}
    assert app_config({"pond": {"type": "app", "memory": "2GB"}, "app": {"command": "run"}})["memory"] == 2 * 10**9
    assert app_config({"pond": {"type": "app"}, "app": {"command": "run", "idle_timeout": "0"}})["idle_timeout_s"] == 0
    (tmp_path / "out").mkdir()
    assert app_config({"pond": {"type": "app"}, "app": {"static": "out"}}, tmp_path)["command"] is None
    for bad in ({"pond": {"type": "app"}},
                {"pond": {"type": "app"}, "app": {"command": "run", "state": "shared"}},
                {"pond": {"type": "app"}, "app": {"command": "run", "health": "healthz"}},
                {"pond": {"type": "app"}, "app": {"command": "run", "idle_timeout": "soon"}},
                {"pond": {"type": "app"}, "app": {"command": "run", "port": 1}},
                {"pond": {"type": "app"}, "app": {"command": "run", "strip_path": "no"}},
                {"pond": {"type": "app", "memory": "lots"}, "app": {"command": "run"}},
                {"pond": {"type": "app"}, "app": {"command": "run", "download": ""}},
                {"pond": {"type": "app"}, "app": {"command": "run", "state": "none", "download": "snap"}},
                {"pond": {}, "app": {"command": "run"}}):
        with pytest.raises(ValueError):
            app_config(bad)
    with pytest.raises(ValueError, match="no directory"):
        app_config({"pond": {"type": "app"}, "app": {"static": "missing"}}, tmp_path)


def test_an_app_has_no_ripples_and_is_never_a_source(catchment_client):
    r = _deploy(catchment_client, "dash", {"pond.toml": _app_toml(sources=""), "main.py": _APP_PY,
                                           "src/pond.py": _SOURCE_PY})
    assert r.status_code == 422 and "no Ripples" in r.json()["detail"]
    assert _deploy(catchment_client, "dash", {"pond.toml": _app_toml(sources=""), "main.py": _APP_PY}).status_code == 200
    sink = '[pond]\nname = "after"\nversion = "1.0.0"\n\n[sources]\ndash = "1.0.0"\n'
    r = _deploy(catchment_client, "after", {"pond.toml": sink, "src/pond.py": _SOURCE_PY})
    assert r.status_code == 422 and "can't be a Source" in r.json()["detail"]


# ─── serving ───────────────────────────────────────────────────────────────────


def test_an_app_serves_its_sources_over_http_and_websockets(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    entry = _app_entry(client)
    assert entry["is_app"] and entry["kind"] == "outlet" and entry["duck"] is None
    assert entry["app"]["path"] == "/apps/dash/v1/" and entry["app"]["instance"] is None
    assert entry["app"]["idle_timeout_s"] == 1800

    r = client.get("/apps/dash/hello?x=1", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/apps/dash/v1/hello?x=1"
    r = client.get("/apps/dash/v1/hello")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base"] == "/apps/dash/v1"
    assert body["state"].endswith("apps/dash/m1/state")
    assert body["headers"]["x-forwarded-proto"] == "http"
    assert body["headers"]["x-forwarded-prefix"] == "/apps/dash/v1"
    assert body["key_env"] is None

    r = client.get("/apps/dash/v1/rows")
    assert r.status_code == 200, r.text
    assert r.json()["n"] == 4 and r.json()["sum"] == 6

    with client.websocket_connect("/apps/dash/v1/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"

    entry = _app_entry(client)
    assert entry["app"]["instance"] is not None
    instances = client.get("/api/ponds/dash/app/instances").json()["instances"]
    assert len(instances) == 1 and instances[0]["ready_at"] and instances[0]["stopped_at"] is None
    log = client.get("/api/ponds/dash/app/logs")
    assert "starting dash@1 1.0.0" in log.text and log.headers["x-log-running"] == "1"
    # Its Sources were seeded, never run, so the App hasn't taken any freshness: no runs yet.
    assert client.get("/api/runs", params={"pond": "dash"}).json()["runs"] == []


def test_an_idle_instance_stops_and_the_next_request_starts_another(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    assert client.get("/apps/dash/v1/hello").status_code == 200
    supervisor = client.app.state.app_supervisor
    inst = supervisor._instances["dash@1"]
    inst.last_used -= 3600  # longer ago than the default idle timeout
    supervisor.reconcile()
    assert inst.proc.poll() is not None and "dash@1" not in supervisor._instances
    assert client.get("/apps/dash/v1/hello").status_code == 200
    instances = client.get("/api/ponds/dash/app/instances").json()["instances"]
    assert [i["stop_reason"] for i in instances] == [None, "idle"]


def test_a_redeploy_replaces_the_instance(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    assert client.get("/apps/dash/v1/hello").status_code == 200
    toml = _app_toml().replace('version = "1.0.0"\ntype', 'version = "1.1.0"\ntype')
    assert _deploy(client, "dash", {"pond.toml": toml, "main.py": _APP_PY}, version="1.1.0").status_code == 200
    client.app.state.app_supervisor.reconcile()
    instances = client.get("/api/ponds/dash/app/instances").json()["instances"]
    assert instances[0]["stop_reason"] == "redeploy"
    assert client.get("/apps/dash/v1/hello").status_code == 200
    assert client.get("/api/ponds/dash/app/instances").json()["instances"][0]["version"] == "1.1.0"


def test_an_app_redeploys_its_running_version_in_place(catchment_client):
    # Nothing pins an App, so the running version can be redeployed without --force; the instance is stopped
    # before its directory is replaced, and the next request starts the new code.
    client = catchment_client
    _deploy_pipeline(client)
    assert client.get("/apps/dash/v1/hello").status_code == 200
    assert client.get("/apps/dash/v1/new").status_code == 404
    new_py = _APP_PY + '\n\n@api.get("/new")\ndef new():\n    return {"new": True}\n'
    assert _deploy(client, "dash", {"pond.toml": _app_toml(), "main.py": new_py}).status_code == 200
    instances = client.get("/api/ponds/dash/app/instances").json()["instances"]
    assert instances[0]["stop_reason"] == "redeploy"
    assert client.get("/apps/dash/v1/new").json() == {"new": True}
    # An instance started before the redeploy's record lands is replaced by the next reconcile.
    supervisor = client.app.state.app_supervisor
    inst = supervisor._instances["dash@1"]
    inst.deployed_at = "an earlier deploy"
    supervisor.reconcile()
    assert inst.proc.poll() is not None and "dash@1" not in supervisor._instances


def test_an_app_that_exits_fails_until_woken(catchment_client):
    client = catchment_client
    _deploy_pipeline(client, {"pond.toml": _app_toml(command="python -c 'import sys; sys.exit(3)'"),
                              "main.py": _APP_PY})
    r = client.get("/apps/dash/v1/hello")
    assert r.status_code == 503 and "exited while starting" in r.text
    entry = _app_entry(client)
    assert entry["is_failed"] and "exit code 3" in entry["error"]
    r = client.get("/apps/dash/v1/hello")
    assert r.status_code == 503 and "failed" in r.text
    assert client.post("/api/ponds/dash/wake").status_code == 200
    assert not _app_entry(client)["is_failed"]
    assert client.get("/apps/dash/v1/hello").status_code == 503  # tried again, and failed again


def test_kill_stops_the_instance(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    assert client.get("/apps/dash/v1/hello").status_code == 200
    inst = client.app.state.app_supervisor._instances["dash@1"]
    assert client.post("/api/ponds/dash/kill").status_code == 200
    assert inst.proc.poll() is not None
    assert client.get("/apps/dash/v1/hello").status_code == 503


def _wait_for(predicate, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_restart_replaces_the_instance_and_reports_it_on_status(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    assert _app_entry(client)["app"]["instance"] is None
    assert client.get("/apps/dash/v1/hello").status_code == 200
    app = _app_entry(client)["app"]
    first = app["instance"]
    assert first["ready_at"] and first["active"] == 0 and first["idle_at"] and app["deployed_at"]
    assert client.post("/api/ponds/dash/app/restart").status_code == 200
    second = _wait_for(lambda: (i := _app_entry(client)["app"]["instance"]) and i["id"] != first["id"]
                       and i["ready_at"] and i)
    assert second["id"] > first["id"]
    reasons = [i["stop_reason"] for i in client.get("/api/ponds/dash/app/instances").json()["instances"]]
    assert reasons[:2] == [None, "restart"]


def test_restart_brings_back_a_killed_app(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    assert client.post("/api/ponds/dash/kill").status_code == 200
    assert _app_entry(client)["is_killed"]
    assert client.post("/api/ponds/dash/app/restart").status_code == 200
    assert not _app_entry(client)["is_killed"]
    _wait_for(lambda: (i := _app_entry(client)["app"]["instance"]) and i["ready_at"])
    assert client.get("/apps/dash/v1/hello").status_code == 200
    assert client.post("/api/ponds/sales/app/restart").status_code == 422  # not an App


def test_an_apps_freshness_follows_its_sources(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    driver = client.app.state.driver
    assert _app_entry(client)["end_f"] is None
    with driver.lock:  # the Source completes a Run: the App's standing Wake takes its freshness
        from datetime import datetime, timezone

        f = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ps = driver.state.pond_states["sales@1"]
        ps.start_f = ps.end_f = ps.changed_f = f
        driver.scheduler_tick()
    assert _app_entry(client)["end_f"] == f.isoformat()
    assert client.get("/apps/dash/v1/rows").json()["fresh"] == str(f)
    with driver.lock:  # and again: each freshness the App takes is one instant run in its history
        g = datetime(2026, 1, 2, tzinfo=timezone.utc)
        ps = driver.state.pond_states["sales@1"]  # a tick replaces the engine state
        ps.start_f = ps.end_f = ps.changed_f = g
        driver.scheduler_tick()
    runs = client.get("/api/runs", params={"pond": "dash"}).json()["runs"]
    assert [r["f"] for r in runs] == [g.isoformat(), f.isoformat()]
    assert all(r["status"] == "success" and r["started_at"] == r["finished_at"] for r in runs)


_ECHO_PY = """async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = (scope["path"] + "?" + scope["query_string"].decode()).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]})
    await send({"type": "http.response.body", "body": body})
"""


def test_strip_path_false_keeps_the_base_path(catchment_client):
    _deploy_pipeline(catchment_client, {
        "pond.toml": _app_toml(command="uvicorn echo:app --port $PORT", extra="strip_path = false\n")
        .replace('health = "/healthz"\n', ""), "echo.py": _ECHO_PY})
    r = catchment_client.get("/apps/dash/v1/a/b%20c?q=1")
    assert r.status_code == 200 and r.text == "/apps/dash/v1/a/b c?q=1"
    assert r.headers.get_list("set-cookie") == ["a=1", "b=2"]


def test_static_files_are_served_by_the_catchment(catchment_client):
    toml = '[pond]\nname = "site"\nversion = "1.0.0"\ntype = "app"\n\n[app]\nstatic = "out"\n'
    r = _deploy(catchment_client, "site", {"pond.toml": toml, "out/index.html": "<h1>hi</h1>",
                                           "out/docs/index.html": "docs", "out/app.js": "x"})
    assert r.status_code == 200, r.text
    assert catchment_client.get("/apps/site/v1/").text == "<h1>hi</h1>"
    assert catchment_client.get("/apps/site/v1/docs/").text == "docs"
    assert catchment_client.get("/apps/site/v1/app.js").text == "x"
    assert catchment_client.get("/apps/site/v1/missing").status_code == 404
    assert catchment_client.get("/apps/site/v1/../../pond.toml").status_code == 404


# ─── access ────────────────────────────────────────────────────────────────────


def test_a_keyed_catchment_gates_apps_and_strips_the_key(tmp_path):
    from duckstring.catchment.app import create_app

    with TestClient(create_app(tmp_path, api_key="full-key")) as client:
        auth = {"Authorization": "Bearer full-key"}
        _deploy_pipeline(client, headers=auth)
        assert client.get("/apps/dash/v1/hello").status_code == 401
        page = client.get("/apps/dash/v1/hello", headers={"accept": "text/html"})
        assert page.status_code == 401 and 'name="key"' in page.text
        r = client.get("/apps/dash/v1/hello", headers=auth)
        assert r.status_code == 200 and "authorization" not in r.json()["headers"]
        r = client.post("/apps/_signin", data={"key": "wrong", "next": "/apps/dash/v1/hello"})
        assert r.status_code == 401
        r = client.post("/apps/_signin", data={"key": "full-key", "next": "/apps/dash/v1/hello"},
                        follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/apps/dash/v1/hello"
        client.cookies.set("duckstring_key", "full-key")
        client.cookies.set("theme", "dark")
        r = client.get("/apps/dash/v1/hello")
        assert r.status_code == 200
        assert r.json()["headers"]["cookie"] == "theme=dark"


# ─── the handle ────────────────────────────────────────────────────────────────


def test_the_handle_reads_sources_and_refuses_writes(tmp_path, monkeypatch):
    import duckstring.app as ds_app

    data = tmp_path / "ponds" / "sales" / "data"
    data.mkdir(parents=True)
    duckdb.connect().execute(f"COPY (SELECT range AS id FROM range(3)) TO '{data / 'revenue.parquet'}' (FORMAT PARQUET)")
    monkeypatch.setenv(ds_app.ENV, json.dumps({"name": "dash", "major": 1, "root": str(tmp_path),
                                               "sources": ["sales"]}))
    monkeypatch.setattr(ds_app, "_handle", None)
    pond = ds_app.pond()
    assert pond is ds_app.pond()
    assert pond.count_table("sales.revenue") == 3
    assert pond.read_table("sales.revenue").aggregate("max(id)").fetchall() == [(2,)]
    assert pond.freshness() == {"freshness": None, "sources": {}}
    with pytest.raises(ValueError, match="'other' is not a Source"):
        pond.read_table("other.revenue")
    with pytest.raises(ValueError, match="reads only its Sources"):
        pond.read_table("revenue")
    with pytest.raises(TypeError):
        pond.write_table("t", pond.con.sql("SELECT 1"))
    with pytest.raises(TypeError):
        pond.read_delta("sales.revenue")

    import threading

    seen = []
    t = threading.Thread(target=lambda: seen.append(pond.con))
    t.start()
    t.join()
    assert seen[0] is not pond.con  # each thread its own cursor
    time.sleep(0)


def test_pond_run_gives_an_app_its_puddles(tmp_path):
    from duckstring.local import load_project
    from duckstring.local.app import app_env

    (tmp_path / "pond.toml").write_text(_app_toml())
    env = app_env(load_project(tmp_path), 8123)
    assert env["PORT"] == "8123" and env["DUCKSTRING_APP_PATH"] == ""
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)  # the environment's commands
    assert env["DUCKSTRING_APP_STATE"] == str(tmp_path / "puddles" / "app" / "state")
    cfg = json.loads(env["DUCKSTRING_APP"])
    assert cfg["root"] == str(tmp_path / "puddles") and cfg["sources"] == ["sales"] and "source_majors" not in cfg


# ─── read leases, Taps and memory slots ───────────────────────────────────────


def _publish(driver, f) -> None:
    """The Source line publishes at ``f`` (the engine's view of it)."""
    with driver.lock:
        ps = driver.state.pond_states["sales@1"]
        ps.start_f = ps.end_f = ps.changed_f = f


def test_a_request_in_flight_keeps_the_source_version_it_may_read(catchment_client):
    from datetime import datetime, timezone

    client = catchment_client
    _deploy_pipeline(client)
    assert client.get("/apps/dash/v1/hello").status_code == 200
    driver, supervisor = client.app.state.driver, client.app.state.app_supervisor
    inst = supervisor._instances["dash@1"]
    f1, f2 = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
    _publish(driver, f1)
    ticket = supervisor.begin(inst)  # a request arrives while f1 is the published version
    _publish(driver, f2)
    with driver.lock:
        assert driver._retain_from("sales@1") == f1.isoformat()  # the publish after f2 keeps f1
    supervisor.end(inst, ticket)
    with driver.lock:
        assert driver._retain_from("sales@1") == f2.isoformat()  # the request ended: only the newest


def test_a_websocket_renews_its_lease_and_keeps_the_previous_one_for_a_while(catchment_client):
    from datetime import datetime, timezone

    from duckstring.catchment.app_supervisor import WS_LEASE_GRACE_S

    client = catchment_client
    _deploy_pipeline(client)
    assert client.get("/apps/dash/v1/hello").status_code == 200
    driver, supervisor = client.app.state.driver, client.app.state.app_supervisor
    inst = supervisor._instances["dash@1"]
    f1, f2, f3 = (datetime(2026, 1, d, tzinfo=timezone.utc) for d in (1, 2, 3))
    _publish(driver, f1)
    ticket = supervisor.begin(inst)  # the connection opens at f1
    _publish(driver, f2)
    supervisor.renew(inst, ticket)  # a message at f2: the work f1's message started may still read f1
    assert sorted(supervisor.leases("dash@1", "sales@1", "sales")) == [f1, f2]
    inst.leases[ticket] = [(t - WS_LEASE_GRACE_S - 1, lease) for t, lease in inst.leases[ticket]]
    _publish(driver, f3)
    supervisor.renew(inst, ticket)  # a minute on: f1 goes, f2 stays as the one before
    assert sorted(supervisor.leases("dash@1", "sales@1", "sales")) == [f2, f3]
    supervisor.end(inst, ticket)
    assert supervisor.leases("dash@1", "sales@1", "sales") == []


def test_hold_keeps_the_versions_current_when_it_began(catchment_client, monkeypatch):
    import duckstring.app as ds_app
    from duckstring.catchment.app_supervisor import app_runtime_dir

    client = catchment_client
    _deploy_pipeline(client)
    assert client.get("/apps/dash/v1/hello").status_code == 200
    root, supervisor = client.app.state.root, client.app.state.app_supervisor
    f = "2026-01-01T00:00:00+00:00"
    (root / "ponds" / "sales" / "m1" / "data" / "_trickle.json").write_text(json.dumps({"_duckstring_run": {"f": f}}))
    monkeypatch.setenv(ds_app.ENV, json.dumps({
        "name": "dash", "major": 1, "root": str(root), "sources": ["sales"], "source_majors": {"sales": 1},
        "runtime": str(app_runtime_dir(root, "dash", 1))}))
    monkeypatch.setattr(ds_app, "_handle", None)
    pond = ds_app.pond()
    with pond.hold():
        assert [d.isoformat() for d in supervisor.leases("dash@1", "sales@1", "sales")] == [f]
    assert supervisor.leases("dash@1", "sales@1", "sales") == []


def test_a_tap_on_an_app_runs_on_its_requests_and_has_no_key(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    r = client.post("/api/ponds/dash/taps", json={"name": "use", "fresh_within_ms": 0})
    assert r.status_code == 200 and r.json()["key"] is None
    assert client.post("/api/ponds/dash/taps/use/rotate").status_code == 422
    assert client.get("/apps/dash/v1/hello").status_code == 200
    tap = client.get("/api/ponds/dash/taps").json()["taps"][0]
    assert tap["query_count"] == 1 and tap["refresh_count"] == 1
    driver = client.app.state.driver
    with driver.lock:  # the Refresh reached the Source, which started a Run for it
        ps = driver.state.pond_states["sales@1"]
        assert ps.demand or ps.start_f > ps.end_f
        driver._flush_tap_counters()
    assert client.get("/apps/dash/v1/hello").status_code == 200
    tap = client.get("/api/ponds/dash/taps").json()["taps"][0]
    assert tap["query_count"] == 2 and tap["refresh_count"] == 1  # a request is already waiting on the Source


def test_an_instance_takes_a_memory_slot_and_its_handle_is_capped(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from duckstring.catchment.app import create_app

    monkeypatch.setenv("DUCKSTRING_RUN_SLOTS", "1")
    monkeypatch.setenv("DUCKSTRING_MEMORY_BUDGET", "2GB")
    with TestClient(create_app(tmp_path)) as client:
        _deploy_pipeline(client)
        driver, supervisor = client.app.state.driver, client.app.state.app_supervisor
        supervisor.start_timeout = 1.0
        with driver.lock:  # the Source has a Run in flight, holding the only slot
            ps = driver.state.pond_states["sales@1"]
            ps.start_f, ps.slots = datetime(2026, 1, 1, tzinfo=timezone.utc), 1
        r = client.get("/apps/dash/v1/mem")
        assert r.status_code == 503 and "waiting for memory" in r.text
        assert not driver.state.pond_states["dash@1"].app_serving  # it left the queue
        with driver.lock:
            ps = driver.state.pond_states["sales@1"]
            ps.start_f, ps.slots = ps.end_f, 0
        supervisor.start_timeout = 60.0
        r = client.get("/apps/dash/v1/mem")
        assert r.status_code == 200, r.text
        assert r.json()["limit"] in ("1.8 GiB", "1.9 GiB", "2.0 GB", "1.8 GB")  # DuckDB's rendering of 2000MB
        assert _app_entry(client)["memory"]["slots"] == 1
        supervisor._instances["dash@1"].last_used -= 3600
        supervisor.reconcile()  # an idle stop gives the slot back
        assert _app_entry(client)["memory"]["slots"] == 0


# ─── downloads ────────────────────────────────────────────────────────────────

_SNAPSHOT = ("python -c \"import os, pathlib; pathlib.Path(os.environ['DUCKSTRING_APP_DOWNLOAD'], 'db.copy')"
             ".write_text(pathlib.Path(os.environ['DUCKSTRING_APP_STATE'], 'db').read_text())\"")


def _archive_names(client) -> tuple[int, dict[str, bytes]]:
    import tarfile

    r = client.get("/api/catchment/archive")
    if r.status_code != 200:
        return r.status_code, {"detail": r.json()["detail"].encode()}
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r") as tar:
        return 200, {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def _app_files(root) -> None:
    state = root / "apps" / "dash" / "m1" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "db").write_text("rows")
    (state / "secret").write_text("token")
    logs = root / "ponds" / "dash" / "m1" / "app" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "1.log").write_text("printed a token")


def test_an_apps_files_stay_out_of_a_download_without_a_download_command(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    _app_files(client.app.state.root)
    status, files = _archive_names(client)
    assert status == 200 and "duck.db" in files
    assert not [n for n in files if n.startswith("apps/") or "/app/" in n]


def test_a_download_carries_what_the_apps_command_writes(catchment_client):
    client = catchment_client
    _deploy_pipeline(client, {"pond.toml": _app_toml(extra=f"download = {json.dumps(_SNAPSHOT)}\n"),
                              "main.py": _APP_PY})
    root = client.app.state.root
    _app_files(root)
    status, files = _archive_names(client)
    assert status == 200
    assert files["apps/dash/m1/state/db.copy"] == b"rows"  # what the command wrote, where a restore wants it
    assert not [n for n in files if n.startswith("apps/") and n != "apps/dash/m1/state/db.copy"]
    assert not [n for n in files if "/app/" in n]  # never its logs
    use = client.get("/api/catchment/usage").json()
    assert use["file_count"] == len([n for n in files if not n.startswith("apps/")])
    # The state backup follows the same rule.
    from duckstring.catchment.state_sync import checkpoint_full, restore_state

    backup = str(root.parent / "backup")
    assert checkpoint_full(root, backup, client.app.state.driver) is True
    fresh = root.parent / "fresh"
    assert restore_state(fresh, backup) is True
    assert (fresh / "apps" / "dash" / "m1" / "state" / "db.copy").read_text() == "rows"
    assert not (fresh / "apps" / "dash" / "m1" / "state" / "secret").exists()
    assert not (fresh / "ponds" / "dash" / "m1" / "app").exists()


def test_a_failing_download_command_fails_the_download(catchment_client):
    client = catchment_client
    failing = "python -c \"print('no database here'); raise SystemExit(4)\""
    _deploy_pipeline(client, {"pond.toml": _app_toml(extra=f"download = {json.dumps(failing)}\n"),
                              "main.py": _APP_PY})
    status, body = _archive_names(client)
    assert status == 500
    assert b"App 'dash' (v1)" in body["detail"] and b"code 4" in body["detail"] and b"no database here" in body["detail"]


def test_bulk_rebuild_and_repair_pass_over_an_app(catchment_client):
    client = catchment_client
    _deploy_pipeline(client)
    r = client.post("/api/ponds/batch", json={"ponds": [{"name": "sales"}, {"name": "dash"}],
                                             "operations": ["rebuild"]})
    assert r.status_code == 200 and r.json()["errors"] == []
    assert not _app_entry(client)["rebuild_pending"]
    sales = next(p for p in client.get("/api/status").json()["ponds"] if p["name"] == "sales")
    assert sales["rebuild_pending"]
    r = client.post("/api/ponds/batch", json={"ponds": [{"name": "dash"}], "operations": ["repair"]})
    assert r.status_code == 200 and r.json()["errors"] == []
    assert not _app_entry(client)["repairing"]
