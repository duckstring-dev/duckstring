"""How many instances an App runs (plans/app-scaling.md): settings and overrides, the default rule, packed routing,
the Catchment as the fallback with growth on the App's own compute, the operator's count, a replaced scaler, and
the App's own load."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from app_remote_launcher import FakeRemoteLauncher
from test_app import _zip
from test_runtime import _free_port, _serve

from duckstring.catchment.app_scaling import AppView, Decision, DefaultScaler, InstanceView, effective

pytestmark = pytest.mark.timeout(180)


# ─── settings and the default rule ─────────────────────────────────────────────


def test_settings_come_from_pond_toml_then_overrides():
    cfg = {"instances": 5, "min_instances": 0, "concurrency": 8, "connections": None, "scale": {"cpu": 0.5}}
    got = effective(cfg, None)
    assert (got["instances"], got["concurrency"], got["connections"]) == (5, 8, 8)  # connections follow concurrency
    assert got["scale"]["cpu"] == 0.5 and got["scale"]["memory"] == 0.8
    assert got["origin"]["instances"] == "pond.toml" and got["origin"]["min_instances"] == "default"
    assert got["origin"]["scale.cpu"] == "pond.toml" and got["origin"]["scale.load"] == "default"
    got = effective(cfg, {"max_instances": 3, "concurrency": 2, "cpu": 0.9, "min_instances": 7})
    assert (got["instances"], got["concurrency"], got["connections"], got["min_instances"]) == (3, 2, 2, 3)
    assert got["scale"]["cpu"] == 0.9 and got["origin"]["scale.cpu"] == "override"
    assert effective({**cfg, "connections": 1}, {"concurrency": 2})["connections"] == 1
    assert effective({}, None)["instances"] == 1  # an App deployed before scaling existed


def _iv(i, **kw):
    base = dict(id=i, where="remote", ready=True, starting=False, draining=False, requests=0, active_sockets=0,
                open_sockets=0, cpu=None, memory=None, load=None, age_s=60.0, idle_s=0.0)
    return InstanceView(**{**base, **kw})


def _app(instances, now=100.0, **kw):
    t = {"requests": 0.7, "cpu": 0.7, "memory": 0.8, "load": 0.7, "after_s": 10.0, "socket_idle_s": 5.0}
    return AppView(key="a@1", now=now, min_instances=0, max_instances=5, concurrency=4, thresholds=t, hold=None,
                   instances=instances, **kw)


def test_the_default_rule_grows_under_sustained_pressure_only():
    scaler = DefaultScaler()
    calm = [_iv(1, requests=1), _iv(2, requests=2)]
    assert scaler.decide(_app(calm)).desired == 2
    busy = [_iv(1, requests=3), _iv(2, requests=3)]  # 3 per instance >= 0.7 x 4
    first = scaler.decide(_app(busy, now=100.0))
    assert first.desired == 2 and "active per instance" in first.reason  # not yet: it must last 10 s
    assert scaler.decide(_app(busy, now=111.0)).desired == 3
    assert scaler.decide(_app(busy, now=112.0)).desired == 2  # the next waits its own 10 s
    # An idle open socket isn't work; a busy one is.
    assert scaler.decide(_app([_iv(1, open_sockets=9)], now=200.0)).desired == 1
    for kind in ("cpu", "memory", "load"):
        s = DefaultScaler()
        hot = [_iv(1, **{kind: 0.95}), _iv(2, **{kind: 0.9})]
        s.decide(_app(hot, now=0.0))
        got = s.decide(_app(hot, now=10.0))
        assert got.desired == 3 and kind in got.reason
    s = DefaultScaler()
    starting = [_iv(1, requests=4), _iv(2, ready=False, starting=True)]
    s.decide(_app(starting, now=0.0))
    assert s.decide(_app(starting, now=20.0)).desired == 2  # one start at a time


# ─── against a live Catchment ──────────────────────────────────────────────────

_APP_PY = '''import os
import time

from fastapi import FastAPI, Response

api = FastAPI()


@api.get("/healthz")
def healthz():
    return {"ok": True}


@api.get("/where")
def where():
    return {"instance": os.environ["DUCKSTRING_APP_INSTANCE"], "concurrency": os.environ["DUCKSTRING_APP_CONCURRENCY"]}


@api.get("/slow")
def slow():
    time.sleep(1.0)
    return {"instance": os.environ["DUCKSTRING_APP_INSTANCE"]}


@api.get("/busy")
def busy(response: Response):
    response.headers["X-Duckstring-Load"] = "0.95"
    return {"instance": os.environ["DUCKSTRING_APP_INSTANCE"]}
'''


def _toml(extra: str = "", scale: str = 'after = "0"\n', duck: str = 'duck = "M"\n') -> str:
    return ('[pond]\nname = "web"\nversion = "1.0.0"\ntype = "app"\n' + duck + '\n[app]\n'
            'command = "uvicorn main:api --port $PORT --root-path $DUCKSTRING_APP_PATH"\nhealth = "/healthz"\n'
            'state = "none"\ninstances = 3\nconcurrency = 2\n' + extra + '\n[app.scale]\n' + scale)


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_DATA_ROOT", str(tmp_path / "data-root"))
    (tmp_path / "data-root").mkdir()
    root = tmp_path / "catchment"
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)
    server, thread = _serve(root, port)
    app = server.config.app
    launcher = FakeRemoteLauncher(url, app.state.duck_token, tmp_path / "machines", str(tmp_path / "data-root"))
    app.state.driver.launcher = launcher

    def deploy(toml: str):
        r = httpx.post(f"{url}/api/deploy", files={"pond": ("pond.zip", _zip({"pond.toml": toml, "main.py": _APP_PY}))},
                       data={"name": "web", "version": "1.0.0", "type": "pond"}, timeout=60)
        assert r.status_code == 200, r.text

    yield url, app, launcher, deploy
    server.should_exit = True
    thread.join(timeout=30)
    launcher.shutdown_all()


def _wait(predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def _rows(url):
    return httpx.get(f"{url}/api/ponds/web/app/scale", timeout=10).json()


def test_the_catchment_serves_first_then_the_app_grows_on_its_own_compute(live):
    url, app, launcher, deploy = live
    deploy(_toml('idle_timeout = "3s"\n'))
    first = httpx.get(f"{url}/apps/web/v1/where", timeout=60).json()
    assert first == {"instance": "1", "concurrency": "2"}
    assert not launcher.spawned  # the Catchment served it: nothing started elsewhere for a quiet App
    assert _rows(url)["instances"][0]["where"] == "catchment"

    async def wave(n):
        async with httpx.AsyncClient(timeout=60) as client:
            return await asyncio.gather(*[client.get(f"{url}/apps/web/v1/slow") for _ in range(n)])

    for _ in range(10):  # busy: another instance starts on the App's own compute
        asyncio.run(wave(4))
        if len(_rows(url)["instances"]) >= 2:
            break
    assert launcher.spawned[0][0] == "web~2@1"  # (it may grow again while the wave lasts: `after` is 0 here)
    assert _wait(lambda: all(r["ready"] for r in _rows(url)["instances"]))
    rows = httpx.get(f"{url}/api/ponds/web/app/instances", timeout=10).json()["instances"]
    assert rows[-2]["place"] == "remote" and "active per instance" in rows[-2]["start_reason"]
    # Packed, instances elsewhere first: new requests go there, and the Catchment's empties and idles out.
    got = {httpx.get(f"{url}/apps/web/v1/where", timeout=30).json()["instance"] for _ in range(3)}
    assert "1" not in got and len(got) == 1
    assert _wait(lambda: {r["where"] for r in _rows(url)["instances"]} == {"remote"}, timeout=30)


def test_a_warm_floor_and_the_operators_count(live):
    url, app, launcher, deploy = live
    deploy(_toml('min_instances = 1\nidle_timeout = "2s"\n'))
    assert _wait(lambda: launcher.spawned)  # the floor starts without a request
    assert httpx.get(f"{url}/apps/web/v1/where", timeout=60).json()["instance"] == "1"
    r = httpx.post(f"{url}/api/ponds/web/app/scale", json={"to": 3, "hold_s": 3600}, timeout=30)
    assert r.status_code == 200, r.text
    assert _wait(lambda: len([r for r in _rows(url)["instances"] if r["ready"]]) == 3, timeout=90)
    time.sleep(3)  # past the idle timeout: a held count doesn't idle out
    assert len(_rows(url)["instances"]) == 3
    r = httpx.post(f"{url}/api/ponds/web/app/scale", json={"to": 1, "hold_s": 3600}, timeout=30)
    assert _wait(lambda: len(_rows(url)["instances"]) == 1, timeout=60)  # drained down to it
    httpx.post(f"{url}/api/ponds/web/app/scale", json={"release": True}, timeout=30)
    assert _rows(url)["hold"] is None
    time.sleep(3)
    assert len(_rows(url)["instances"]) == 1  # never below the floor
    # Overrides, kept on the line.
    state = httpx.post(f"{url}/api/ponds/web/app/scale", json={"max_instances": 2, "cpu": 0.5}, timeout=30).json()
    assert state["settings"]["instances"] == 2 and state["origin"]["instances"] == "override"
    assert state["settings"]["scale"]["cpu"] == 0.5
    state = httpx.post(f"{url}/api/ponds/web/app/scale", json={"clear": True}, timeout=30).json()
    assert state["settings"]["instances"] == 3 and state["origin"]["instances"] == "pond.toml"
    assert httpx.post(f"{url}/api/ponds/web/app/scale", json={"min_instances": 5}, timeout=30).status_code == 422
    # Draining an instance by hand: the floor brings another.
    first = _rows(url)["instances"][0]["id"]
    assert httpx.post(f"{url}/api/ponds/web/app/instances/{first}/drain", timeout=30).status_code == 200
    assert _wait(lambda: [r["id"] for r in _rows(url)["instances"]] not in ([first], []), timeout=90)


class TwoScaler:
    """A replaced scaler: always two, and drain anything past that."""

    def decide(self, app: AppView) -> Decision:
        live = [i for i in app.instances if not i.draining]
        return Decision(desired=2, drain=[i.id for i in live[2:]], reason="two, always")


def test_a_replaced_scaler_decides(live, monkeypatch):
    url, app, launcher, deploy = live
    monkeypatch.setattr(app.state.app_supervisor, "scaler", TwoScaler())
    deploy(_toml(duck=""))  # on the Catchment
    assert _wait(lambda: len([r for r in _rows(url)["instances"] if r["ready"]]) == 2, timeout=60)
    assert all(r["where"] == "catchment" for r in _rows(url)["instances"])
    assert _rows(url)["decisions"][-1]["reason"] == "two, always"


def test_the_apps_own_load_grows_it(live):
    url, app, launcher, deploy = live
    deploy(_toml(duck="", scale='after = "0"\nload = 0.5\n'))
    r = httpx.get(f"{url}/apps/web/v1/busy", timeout=60)
    assert r.status_code == 200 and "x-duckstring-load" not in r.headers  # read, and kept from the browser
    assert _wait(lambda: len(_rows(url)["instances"]) >= 2, timeout=60)
    assert any("load" in (d["reason"] or "") for d in _rows(url)["decisions"])


def test_the_scaling_command(live, monkeypatch):
    import sys

    from typer.testing import CliRunner

    import duckstring.cli.app  # noqa: F401

    mod = cli_app = sys.modules["duckstring.cli.app"]
    url, app, launcher, deploy = live
    deploy(_toml(duck=""))
    httpx.get(f"{url}/apps/web/v1/where", timeout=60)

    def resolve(_):
        return url, {"url": url}

    monkeypatch.setattr(mod, "_resolve", resolve)
    out = CliRunner().invoke(cli_app.app, ["scale", "web", "--max", "2"])
    assert out.exit_code == 0, out.output
    assert "instances 0 to 2 (override)" in out.output and "Catchment" in out.output
    out = CliRunner().invoke(cli_app.app, ["scale", "web", "--clear"])
    assert "to 3 (pond.toml)" in out.output, out.output
    out = CliRunner().invoke(cli_app.app, ["instances", "web"])
    assert "Catchment" in out.output, out.output
    assert json.loads(json.dumps(_rows(url)))["pond"] == "web@1"


def test_scaling_shows_in_status_metrics_and_an_opt_in_alert(live):
    url, app, launcher, deploy = live
    r = httpx.post(f"{url}/api/alerts", json={"name": "scale", "destination": "http://127.0.0.1:9/hook",
                                              "events": "app_scale"}, timeout=10)
    assert r.status_code == 200, r.text
    httpx.post(f"{url}/api/alerts", json={"name": "ops", "destination": "http://127.0.0.1:9/ops"}, timeout=10)
    deploy(_toml('min_instances = 1\n'))
    assert _wait(lambda: any(r["ready"] for r in _rows(url)["instances"]), timeout=90)
    status = next(p for p in httpx.get(f"{url}/api/status", timeout=10).json()["ponds"] if p["name"] == "web")
    assert status["app"]["instance"]["count"] == 1 and status["app"]["instance"]["elsewhere"] == 1
    text = httpx.get(f"{url}/metrics", timeout=10).text
    assert 'duckstring_app_instances{pond="web",major="1",place="elsewhere"} 1' in text
    assert 'duckstring_app_instance_starts_total{pond="web",major="1",place="elsewhere"} 1' in text
    assert 'duckstring_app_at_maximum{pond="web",major="1"} 0' in text
    sent = httpx.get(f"{url}/api/alerts/deliveries", timeout=10).json()
    rows = sent if isinstance(sent, list) else sent.get("deliveries", [])
    kinds = {(d.get("channel") or d.get("channel_name"), d.get("event_kind") or d.get("kind")) for d in rows}
    assert any(k == "app_scale" for _, k in kinds), rows  # the channel that asked for it
    assert not any(c == "ops" and k == "app_scale" for c, k in kinds)  # never part of `all`


def test_an_instance_is_replaced_before_its_computes_time_limit(live, monkeypatch):
    url, app, launcher, deploy = live
    deploy(_toml('min_instances = 1\n'))
    assert _wait(lambda: any(r["ready"] for r in _rows(url)["instances"]), timeout=90)
    monkeypatch.setattr(launcher, "max_lifetime", lambda duck: 30.0, raising=False)  # 90% of it: 27 s
    supervisor = app.state.app_supervisor
    (inst,) = supervisor._of("web@1")
    if hasattr(inst, "_limit"):
        del inst._limit
    inst.started -= 60  # as if it had run a minute
    assert _wait(lambda: any(d["reason"].startswith("replacing #1") for d in _rows(url)["decisions"]), timeout=10)
    assert _wait(lambda: [r["id"] for r in _rows(url)["instances"] if r["ready"]] == [2], timeout=90)


def test_growth_stops_at_the_postgresql_connection_budget():
    from types import SimpleNamespace

    from duckstring.catchment.app_supervisor import AppSupervisor, Instance

    class Records:
        def max_connections(self):
            return 20

    views = {"a@1": {"app": {"records": True}, "scaling": {"connections": 4}},
             "b@1": {"app": {"records": True}, "scaling": {"connections": 3}},
             "c@1": {"app": {"records": False}, "scaling": {"connections": 50}}}
    driver = SimpleNamespace(records=Records(), app_view=views.get, on_app_stop=None, app_leases=None,
                             app_activity=None, app_metrics=None)
    sup = AppSupervisor(driver, "/tmp")

    def inst(key, i):
        return Instance(key=key, id=i, version_id=1, deployed_at=None, proc=None, port=0, log_path=None)

    sup._instances = {"a@1": [inst("a@1", 1)], "b@1": [inst("b@1", 2)], "c@1": [inst("c@1", 3)]}
    # used 4 + 3 = 7 (c has no Records); 10 kept back: 7 + 4 <= 10 is false
    assert not sup._connections_allow(views["a@1"])
    Records.max_connections = lambda self: 21
    assert sup._connections_allow(views["a@1"])
    assert sup._connections_allow(views["c@1"])
