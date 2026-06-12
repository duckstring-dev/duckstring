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


def _serve(root, port):
    """Start a Catchment uvicorn server on (root, port); return (server, thread) once healthy."""
    from duckstring.catchment.app import create_app

    config = uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("Catchment did not start")
    return server, thread


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def runtime(tmp_path_factory, monkeypatch):
    """A real uvicorn Catchment with Duck spawning ENABLED, reachable by the spawned subprocesses."""
    root = tmp_path_factory.mktemp("runtime_root")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)  # enable real Ducks
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)

    server, thread = _serve(root, port)
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

    httpx.post(f"{url}/api/ponds/reports/pulse", timeout=5.0)

    # The whole chain runs and reports reaches a freshness (end_f).
    assert _wait(lambda: (_pond_status(url, "reports") or {}).get("end_f") is not None), \
        "reports never became fresh"

    # Every pond produced a run ledger recording a successful Pond Run.
    from duckstring.engine import pond as ledger
    for name in _PONDS:
        db_path = root / "ponds" / name / "m1" / "pond.db"
        assert db_path.exists(), f"no ledger for {name}"
        con = ledger.connect(db_path)
        assert ledger.read_pond_end_f(con) is not None, f"{name} recorded no completed run"
        con.close()

    # Coherent: under a Pulse the outlet and its source both reach a freshness.
    reports = _pond_status(url, "reports")
    sales = _pond_status(url, "sales")
    assert reports["end_f"] is not None and sales["end_f"] is not None


def test_wave_then_remove(runtime):
    url, root = runtime
    _deploy_demo(url)

    httpx.post(f"{url}/api/ponds/reports/wave", timeout=5.0)

    # A Wave keeps producing runs: reports completes several times.
    rep_db = root / "ponds" / "reports" / "m1" / "pond.db"
    from duckstring.engine import pond as ledger

    def completed_runs() -> int:
        if not rep_db.exists():
            return 0
        con = ledger.connect(rep_db)
        n = con.execute("SELECT COUNT(*) FROM pond_run WHERE status = 'success'").fetchone()[0]
        con.close()
        return n

    assert _wait(lambda: completed_runs() >= 2), "wave did not produce repeated runs"

    # Removing the standing trigger halts the Wave; in-flight runs drain, then it stabilises.
    httpx.post(f"{url}/api/ponds/reports/untrigger", timeout=5.0)
    time.sleep(2.0)
    settled = completed_runs()
    time.sleep(2.0)
    assert completed_runs() == settled


def test_restart_restores_state_e2e(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("restart_root")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)

    # First Catchment: deploy + pulse the chain to completion.
    server, thread = _serve(root, port)
    try:
        _deploy_demo(url)
        httpx.post(f"{url}/api/ponds/reports/pulse", timeout=5.0)
        assert _wait(lambda: (_pond_status(url, "reports") or {}).get("end_f") is not None)
        before = _pond_status(url, "reports")
        assert before["gen"] >= 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    # Restart on the SAME root + DB: state must be restored, not fresh.
    server2, thread2 = _serve(root, port)
    try:
        after = _pond_status(url, "reports")
        assert after["gen"] == before["gen"], "gen reset on restart"
        assert after["end_f"] is not None, "freshness lost on restart"
    finally:
        server2.should_exit = True
        thread2.join(timeout=5)


def test_status_and_runs_feed_live(runtime):
    """The UI's read surface against a real run: the enriched /api/status (ripple-level state +
    intra-Pond edges, d_ms, trigger) and the /api/runs history (lineage filter + nested Ripple Runs).
    """
    url, root = runtime
    _deploy_demo(url)

    httpx.post(f"{url}/api/ponds/reports/pulse", timeout=5.0)
    assert _wait(lambda: (_pond_status(url, "reports") or {}).get("end_f") is not None)

    # Enriched status: every Pond carries d_ms + a ripple list; sales has intra-Pond edges
    # (join_lines depends on daily_sales + price_tiers).
    reports = _pond_status(url, "reports")
    assert isinstance(reports["d_ms"], int) and reports["trigger"] is None
    assert {r["name"] for r in reports["ripples"]}  # non-empty
    sales = _pond_status(url, "sales")
    assert ["daily_sales", "join_lines"] in sales["ripple_edges"]
    assert ["price_tiers", "join_lines"] in sales["ripple_edges"]

    # Global run feed includes reports; nested Ripple Runs appear only when requested.
    runs = httpx.get(f"{url}/api/runs", params={"ripples": True}, timeout=5.0).json()["runs"]
    rep_run = next(r for r in runs if r["pond"] == "reports")
    assert rep_run["status"] == "success"
    assert any(rr["status"] == "success" for rr in rep_run["ripples"])
    # Ripple Runs carry a real execution span (started_at + finished_at) → a duration for the UI.
    rr = rep_run["ripples"][0]
    assert rr["started_at"] is not None and rr["finished_at"] is not None
    assert rr["finished_at"] >= rr["started_at"]

    # Lineage filter: reports + its upstream sources (sales, transactions, products) — not just reports.
    feed = httpx.get(f"{url}/api/runs", params={"pond": "reports", "lineage": True}, timeout=5.0).json()["runs"]
    ponds_seen = {r["pond"] for r in feed}
    assert "sales" in ponds_seen and "transactions" in ponds_seen
    only = httpx.get(f"{url}/api/runs", params={"pond": "reports", "lineage": False}, timeout=5.0).json()["runs"]
    assert {r["pond"] for r in only} == {"reports"}
