"""API-key auth: a Catchment started with a key rejects unauthenticated /api requests; the CLI sends
the key stored against the registered catchment; Ducks present it as their X-Duck-Token."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from duckstring.catchment.app import create_app
from duckstring.cli import app as cli_app

pytestmark = pytest.mark.timeout(10)

KEY = "sekret-key"


@pytest.fixture
def keyed_client(tmp_path):
    with TestClient(create_app(tmp_path, api_key=KEY)) as client:
        yield client


def test_requests_without_key_rejected(keyed_client):
    assert keyed_client.get("/api/status").status_code == 401
    assert keyed_client.post("/api/ponds/x/pulse").status_code == 401
    assert keyed_client.post("/api/deploy").status_code == 401


def test_bearer_and_duck_token_accepted(keyed_client):
    assert keyed_client.get("/api/status", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
    assert keyed_client.get("/api/status", headers={"X-Duck-Token": KEY}).status_code == 200
    assert keyed_client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_health_stays_open(keyed_client):
    assert keyed_client.get("/api/health").status_code == 200


def test_no_key_means_open(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/status").status_code == 200


def test_env_var_sets_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DUCKSTRING_API_KEY", KEY)
    with TestClient(create_app(tmp_path / "envroot")) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


# ─── CLI round-trip against a live keyed Catchment ───────────────────────────────


@pytest.fixture
def keyed_catchment(tmp_path_factory):
    root = tmp_path_factory.mktemp("keyed_root")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config(create_app(root, api_key=KEY), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(timeout=5)


def test_cli_sends_stored_key(runner, keyed_catchment):
    from duckstring.cli.config import register_catchment, set_default_catchment

    register_catchment("keyed", url=keyed_catchment, kind="remote", key=KEY)
    set_default_catchment("keyed")
    result = runner.invoke(cli_app, ["status", "--once"])
    assert result.exit_code == 0, result.output


def test_cli_without_key_gets_friendly_401(runner, keyed_catchment):
    from duckstring.cli.config import register_catchment, set_default_catchment

    register_catchment("unkeyed", url=keyed_catchment, kind="remote")
    set_default_catchment("unkeyed")
    result = runner.invoke(cli_app, ["status", "--once"])
    assert result.exit_code == 1
    assert "401" in result.output


def test_connect_stores_key(runner, keyed_catchment):
    from duckstring.cli.config import load_config

    result = runner.invoke(
        cli_app,
        ["catchment", "connect", "--name", "rk", "--path", keyed_catchment, "--key", KEY, "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert load_config()["catchments"]["rk"]["key"] == KEY


# ─── Ducks authenticate with the Catchment's key ─────────────────────────────────


@pytest.mark.timeout(60)
def test_duck_authenticates_e2e(tmp_path_factory, monkeypatch):
    """A keyed Catchment passes its key to spawned Ducks as their token — a real run completes."""
    import io
    import zipfile

    root = tmp_path_factory.mktemp("keyed_duck_root")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)  # real Ducks
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)

    config = uvicorn.Config(create_app(root, api_key=KEY), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)

    headers = {"Authorization": f"Bearer {KEY}"}
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("pond.toml", '[pond]\nname = "inlet"\nversion = "1.0.0"\ntype = "inlet"\n')
            zf.writestr(
                "src/pond.py",
                "from duckstring import ripple\n\n"
                "@ripple\n"
                "def make(pond):\n"
                "    pond.write_table('event', pond.con.sql('SELECT 1 AS id'))\n",
            )
        r = httpx.post(
            f"{url}/api/deploy",
            files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
            data={"name": "inlet", "version": "1.0.0", "type": "inlet"},
            headers=headers, timeout=15.0,
        )
        assert r.status_code == 200, r.text

        httpx.post(f"{url}/api/ponds/inlet/pulse", headers=headers, timeout=5.0)

        def fresh() -> bool:
            ponds = httpx.get(f"{url}/api/status", headers=headers, timeout=5.0).json()["ponds"]
            p = next((x for x in ponds if x["id"] == "inlet@1"), None)
            return p is not None and p.get("end_f") is not None

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not fresh():
            time.sleep(0.25)
        assert fresh(), "the Duck should authenticate and complete the run"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ─── Custom headers (platform auth in front of the Catchment) ────────────────────


def test_cli_sends_custom_headers(runner, keyed_catchment):
    # No `key` on the registration — the Authorization header comes from the custom headers table,
    # exactly as it would for a platform gate (e.g. 'Authorization: Key …' on Posit Connect).
    from duckstring.cli.config import register_catchment, set_default_catchment

    register_catchment("hdr", url=keyed_catchment, kind="remote", headers={"Authorization": f"Bearer {KEY}"})
    set_default_catchment("hdr")
    result = runner.invoke(cli_app, ["status", "--once"])
    assert result.exit_code == 0, result.output


def test_connect_header_flag_stores_headers(runner, keyed_catchment):
    from duckstring.cli.config import load_config

    result = runner.invoke(
        cli_app,
        ["catchment", "connect", "--name", "ph", "--path", keyed_catchment,
         "--header", f"Authorization: Bearer {KEY}", "--header", "X-Extra: 1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert load_config()["catchments"]["ph"]["headers"] == {
        "Authorization": f"Bearer {KEY}", "X-Extra": "1",
    }
    result = runner.invoke(cli_app, ["status", "--once", "-c", "ph"])
    assert result.exit_code == 0, result.output


def test_connect_malformed_header_rejected(runner):
    result = runner.invoke(
        cli_app,
        ["catchment", "connect", "--name", "bad", "--path", "http://x", "--header", "no-colon", "--yes"],
    )
    assert result.exit_code == 1
    assert "invalid --header" in result.output


def test_auth_headers_merging():
    from duckstring.cli.config import auth_headers

    # key alone → Bearer; explicit Authorization header wins over the key; others pass through.
    assert auth_headers({"key": "k"}) == {"Authorization": "Bearer k"}
    assert auth_headers({"key": "k", "headers": {"Authorization": "Key posit"}}) == {"Authorization": "Key posit"}
    assert auth_headers({"headers": {"X-A": "1"}, "key": "k"}) == {"X-A": "1", "Authorization": "Bearer k"}
    assert auth_headers({}) == {}


def test_config_file_is_private(runner, keyed_catchment):
    from duckstring.cli.config import CONFIG_FILE, register_catchment

    register_catchment("perm", url=keyed_catchment, kind="remote", key=KEY)
    assert (CONFIG_FILE.stat().st_mode & 0o777) == 0o600


# ─── --generate-key ───────────────────────────────────────────────────────────────


def test_init_generate_key(runner, tmp_path, mock_uvicorn):
    from duckstring.cli.config import load_config

    result = runner.invoke(
        cli_app,
        ["catchment", "init", "--name", "gen", "--root", str(tmp_path / "gen"), "--generate-key", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "Access keys" in result.output
    # The ladder prints all three levels; the full key is stored so `catchment start gen` reuses it.
    for level in ("full", "demand", "read"):
        assert level in result.output
    stored = load_config()["catchments"]["gen"].get("key")
    assert stored and len(stored) >= 24

    # The full key authenticates; the three hashes are persisted in the Catchment db.
    from duckstring.catchment import auth
    from duckstring.catchment.db import connect

    con = connect(tmp_path / "gen" / "duck.db")
    levels = {row[0] for row in con.execute("SELECT level FROM catchment_key")}
    assert levels == set(auth.NAME_TO_LEVEL)


def test_init_generate_key_conflicts_with_key(runner, tmp_path):
    result = runner.invoke(
        cli_app,
        ["catchment", "init", "--name", "g2", "--root", str(tmp_path / "g2"),
         "--generate-key", "--key", "explicit", "--yes"],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
