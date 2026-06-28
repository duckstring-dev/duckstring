"""The access-level key ladder (read ⊂ demand ⊂ full), key rotation, and the decoupled Duck token.

These exercise authorization (what a level may do), distinct from test_auth.py's authentication (is a
key present/valid). The engine is disabled — only the HTTP gate is under test."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from duckstring.catchment import auth
from duckstring.catchment.app import create_app

pytestmark = pytest.mark.timeout(10)


@pytest.fixture
def laddered(tmp_path):
    """A Catchment with the three-tier ladder minted; yields (client, keys)."""
    app = create_app(tmp_path)
    keys = auth.generate(app.state.db)
    with TestClient(app) as client:
        yield client, keys


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ─── The ladder: read ⊂ demand ⊂ full ────────────────────────────────────────


def test_read_key_reads_but_cannot_demand_or_control(laddered):
    client, keys = laddered
    h = _bearer(keys["read"])
    assert client.get("/api/status", headers=h).status_code == 200          # read: ok
    assert client.post("/api/ponds/x/tap", headers=h).status_code == 403    # demand: forbidden
    assert client.post("/api/deploy", headers=h).status_code == 403         # full: forbidden


def test_demand_key_can_solicit_but_not_control(laddered):
    client, keys = laddered
    h = _bearer(keys["demand"])
    assert client.get("/api/status", headers=h).status_code == 200          # read ⊂ demand
    # tap reaches the engine (404: pond 'x' undeployed) — i.e. it passed the demand gate, not 403.
    assert client.post("/api/ponds/x/tap", headers=h).status_code == 404
    assert client.post("/api/deploy", headers=h).status_code == 403         # full: forbidden
    assert client.post("/api/ponds/x/kill", headers=h).status_code == 403   # control is full


def test_full_key_passes_every_gate(laddered):
    client, keys = laddered
    h = _bearer(keys["full"])
    assert client.get("/api/status", headers=h).status_code == 200
    assert client.post("/api/ponds/x/tap", headers=h).status_code == 404    # authorized → engine 404
    assert client.post("/api/ponds/x/kill", headers=h).status_code == 404   # authorized → engine 404


def test_unauthenticated_is_401_not_403(laddered):
    client, _ = laddered
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=_bearer("nonsense")).status_code == 401


def test_legacy_single_key_is_full(tmp_path):
    """A bare `api_key` (no ladder) still works and means full access."""
    with TestClient(create_app(tmp_path, api_key="solo")) as client:
        h = _bearer("solo")
        assert client.get("/api/status", headers=h).status_code == 200
        # A full-only route succeeds (rotate even mints the ladder from a legacy single-key start).
        assert client.post("/api/catchment/keys/rotate", headers=h, json={}).status_code == 200


# ─── The level signal + traceback redaction ──────────────────────────────────


def test_status_reports_callers_access_level(laddered):
    client, keys = laddered
    for level in ("read", "demand", "full"):
        payload = client.get("/api/status", headers=_bearer(keys[level])).json()
        assert payload["access_level"] == level


def test_status_access_level_is_full_in_open_mode(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/status").json()["access_level"] == "full"


def test_runs_redacts_traceback_below_full(laddered, monkeypatch):
    client, keys = laddered

    # Stub the run feed with a traceback-bearing run + nested ripple.
    run = {"pond": "x", "id": "x@1", "error": "boom", "traceback": "Traceback…secret/path",
           "ripples": [{"ripple": "r", "error": "boom", "traceback": "Traceback…secret/path"}]}
    monkeypatch.setattr(client.app.state.driver, "run_history", lambda *a, **k: [dict(run, ripples=[dict(run["ripples"][0])])])

    for level in ("read", "demand"):
        got = client.get("/api/runs", headers=_bearer(keys[level])).json()["runs"][0]
        assert got["error"] == "boom"            # the message survives every level
        assert got["traceback"] is None          # the traceback is redacted
        assert got["ripples"][0]["traceback"] is None

    full = client.get("/api/runs", headers=_bearer(keys["full"])).json()["runs"][0]
    assert full["traceback"] == "Traceback…secret/path"
    assert full["ripples"][0]["traceback"] == "Traceback…secret/path"


# ─── Rotation ────────────────────────────────────────────────────────────────


def test_rotate_requires_full(laddered):
    client, keys = laddered
    assert client.post("/api/catchment/keys/rotate", headers=_bearer(keys["read"]), json={}).status_code == 403
    assert client.post("/api/catchment/keys/rotate", headers=_bearer(keys["demand"]), json={}).status_code == 403


def test_rotate_subset_invalidates_old_keeps_others(laddered):
    client, keys = laddered
    r = client.post("/api/catchment/keys/rotate", headers=_bearer(keys["full"]), json={"levels": ["read"]})
    assert r.status_code == 200
    new_read = r.json()["keys"]["read"]
    assert set(r.json()["keys"]) == {"read"}  # only the requested level rerolled

    assert client.get("/api/status", headers=_bearer(keys["read"])).status_code == 401     # old read dead
    assert client.get("/api/status", headers=_bearer(new_read)).status_code == 200         # new read works
    assert client.get("/api/status", headers=_bearer(keys["full"])).status_code == 200     # full untouched


def test_rotate_all_default(laddered):
    client, keys = laddered
    r = client.post("/api/catchment/keys/rotate", headers=_bearer(keys["full"]), json={})
    assert r.status_code == 200
    assert set(r.json()["keys"]) == set(auth.NAME_TO_LEVEL)
    # every old key is now invalid; the new full key authorizes
    for old in keys.values():
        assert client.get("/api/status", headers=_bearer(old)).status_code == 401
    assert client.get("/api/status", headers=_bearer(r.json()["keys"]["full"])).status_code == 200


def test_rotate_rejects_unknown_level(laddered):
    client, keys = laddered
    r = client.post("/api/catchment/keys/rotate", headers=_bearer(keys["full"]), json={"levels": ["admin"]})
    assert r.status_code == 422


# ─── The Duck channel uses its own token, decoupled from user keys ────────────


def test_duck_channel_needs_worker_token_not_user_key(laddered):
    client, keys = laddered
    app = client.app
    # The worker token is not any user key, and a user key cannot use the duck channel.
    assert app.state.duck_token not in keys.values()
    assert client.get("/api/duck/x/1/jobs").status_code == 401
    assert client.get("/api/duck/x/1/jobs", headers=_bearer(keys["full"])).status_code == 401
    ok = client.get("/api/duck/x/1/jobs", headers={"X-Duck-Token": app.state.duck_token})
    assert ok.status_code == 200


def test_duck_token_persists_across_restart(tmp_path):
    """A Duck that outlives a Catchment restart keeps authenticating — the token is persisted."""
    app1 = create_app(tmp_path)
    token1 = app1.state.duck_token
    app1.state.db.close()
    app2 = create_app(tmp_path)
    assert app2.state.duck_token == token1


def test_rotating_user_keys_leaves_duck_token(laddered):
    client, keys = laddered
    token = client.app.state.duck_token
    client.post("/api/catchment/keys/rotate", headers=_bearer(keys["full"]), json={})
    assert client.app.state.duck_token == token
    assert client.get("/api/duck/x/1/jobs", headers={"X-Duck-Token": token}).status_code == 200


# ─── Open mode (no auth configured) stays fully open, duck channel included ───


def test_open_mode_allows_everything(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/ponds/x/tap").status_code == 404       # reached engine, not gated
        assert client.get("/api/duck/x/1/jobs").status_code == 200      # duck channel ungated when open


# ─── CLI rotate-keys against a live Catchment ────────────────────────────────


@pytest.fixture
def laddered_server(tmp_path_factory):
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    root = tmp_path_factory.mktemp("ladder_root")
    app = create_app(root)
    keys = auth.generate(app.state.db)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    yield url, keys
    server.should_exit = True
    thread.join(timeout=5)


def test_cli_rotate_keys_updates_stored_full_key(runner, laddered_server):
    from duckstring.cli import app as cli_app
    from duckstring.cli.config import load_config, register_catchment, set_default_catchment

    url, keys = laddered_server
    register_catchment("rk", url=url, kind="remote", key=keys["full"])
    set_default_catchment("rk")

    result = runner.invoke(cli_app, ["catchment", "rotate-keys", "--yes"])
    assert result.exit_code == 0, result.output

    new_full = load_config()["catchments"]["rk"]["key"]
    assert new_full != keys["full"]  # the stored full key was replaced, so the CLI keeps working

    # The rotated full key authenticates; the old one no longer does.
    import httpx

    assert httpx.get(f"{url}/api/status", headers={"Authorization": f"Bearer {new_full}"}).status_code == 200
    assert httpx.get(f"{url}/api/status", headers={"Authorization": f"Bearer {keys['full']}"}).status_code == 401
