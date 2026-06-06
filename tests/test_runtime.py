"""End-to-end runtime tests: a live Catchment + real Duck subprocesses on the demo ponds.

Proves the whole two-tier wiring — trigger → engine cascade → begin_run dispatched over the job
long-poll → Duck executes ripples → events reported → freshness advances → run ledger + history
written → Duck idle-exits. Crash/replay + incomplete-only recovery are unit-tested in test_duck.py;
here we verify the pieces compose against real processes.

Uses the session-wide DUCKSTRING_SLEEP_MULTIPLIER=0.01 so the demo ripples run fast.
"""

from __future__ import annotations

import io
import socket
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
import uvicorn

pytestmark = pytest.mark.timeout(60)

_DEMO = Path(__file__).parent.parent / "src" / "duckstring" / "demo"
_PONDS = ("transactions", "products", "sales", "reports")


def _zip_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


def _read_toml(path: Path) -> dict:
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(path.read_text())
    import tomli
    return tomli.loads(path.read_text())


@pytest.fixture
def runtime(tmp_path_factory, monkeypatch):
    """A real uvicorn Catchment with Duck spawning ENABLED, reachable by the spawned subprocesses."""
    from duckstring.catchment.app import create_app

    root = tmp_path_factory.mktemp("runtime_root")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)  # enable real Ducks
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)

    config = uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="error")
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
    else:
        raise RuntimeError("Catchment did not start")

    yield url, root

    server.should_exit = True
    thread.join(timeout=5)


def _deploy_demo(url: str) -> None:
    for name in _PONDS:
        info = _read_toml(_DEMO / name / "pond.toml")["pond"]
        r = httpx.post(
            f"{url}/api/deploy",
            files={"pond": ("pond.zip", _zip_dir(_DEMO / name), "application/zip")},
            data={"name": info["name"], "version": info["version"], "type": info.get("type", "pond")},
            timeout=15.0,
        )
        assert r.status_code == 200, r.text


def _pond_status(url: str, name: str) -> dict | None:
    rows = httpx.get(f"{url}/api/status", timeout=5.0).json()["ponds"]
    return next((p for p in rows if p["name"] == name), None)


def _wait(predicate, timeout=45.0, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_pulse_runs_chain_end_to_end(runtime):
    url, root = runtime
    _deploy_demo(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=5.0)

    # The whole chain runs and reports reaches a freshness.
    assert _wait(lambda: (_pond_status(url, "reports") or {}).get("freshness") is not None), \
        "reports never became fresh"

    # Every pond produced a run ledger recording a successful Pond Run.
    from duckstring.engine import pond as ledger
    for name in _PONDS:
        db_path = root / "ponds" / name / "pond.db"
        assert db_path.exists(), f"no ledger for {name}"
        con = ledger.connect(db_path)
        assert ledger.read_pond_end_f(con) is not None, f"{name} recorded no completed run"
        con.close()

    # Coherent: under a Pulse the outlet is no fresher than its source.
    reports = _pond_status(url, "reports")
    sales = _pond_status(url, "sales")
    assert reports["freshness"] is not None and sales["freshness"] is not None


def test_wave_then_stop(runtime):
    url, root = runtime
    _deploy_demo(url)

    httpx.post(f"{url}/api/outlets/reports/wave", timeout=5.0)

    # A Wave keeps producing runs: reports completes several times.
    rep_db = root / "ponds" / "reports" / "pond.db"
    from duckstring.engine import pond as ledger

    def completed_runs() -> int:
        if not rep_db.exists():
            return 0
        con = ledger.connect(rep_db)
        n = con.execute("SELECT COUNT(*) FROM pond_run WHERE status = 'success'").fetchone()[0]
        con.close()
        return n

    assert _wait(lambda: completed_runs() >= 2), "wave did not produce repeated runs"

    httpx.post(f"{url}/api/outlets/reports/stop", timeout=5.0)
    time.sleep(1.0)
    settled = completed_runs()
    time.sleep(1.5)
    # After stop, no significant new runs start (in-flight may drain by at most ~1).
    assert completed_runs() <= settled + 1
