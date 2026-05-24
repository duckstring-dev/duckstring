from __future__ import annotations

import io
import socket
import sqlite3
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest
import uvicorn

_DEMO_DIR = Path(__file__).parent.parent / "src" / "duckstring" / "demo"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def orch_catchment(tmp_path_factory, isolated_config):
    """Yield (url, root). root is needed for direct DB access after execution."""
    from duckstring.catchment.app import create_app
    from duckstring.cli.config import register_catchment, set_default_catchment

    root = tmp_path_factory.mktemp("orch_root")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"Orchestration catchment did not start on port {port}")

    register_catchment("dev", url=url, kind="local")
    set_default_catchment("dev")

    yield url, root

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_toml(path: Path) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8"))
    import tomli
    return tomli.loads(path.read_text(encoding="utf-8"))


def _zip_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


def _deploy_demo(url: str, name: str) -> None:
    pond_dir = _DEMO_DIR / name
    info = _read_toml(pond_dir / "pond.toml")["pond"]
    r = httpx.post(
        f"{url}/api/deploy",
        files={"pond": ("pond.zip", _zip_dir(pond_dir), "application/zip")},
        data={"name": info["name"], "version": info["version"], "type": info.get("type", "pond")},
        timeout=30.0,
    )
    assert r.status_code == 200, f"Deploy of {name} failed: {r.text}"


def _deploy_all(url: str) -> None:
    for name in ("transactions", "products", "sales", "reports"):
        _deploy_demo(url, name)


def _wait_for(
    db_path: Path,
    condition_sql: str,
    timeout: float = 60.0,
    poll: float = 0.1,
) -> None:
    """Block until condition_sql returns a truthy scalar."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        con = sqlite3.connect(str(db_path))
        try:
            ok = con.execute(condition_sql).fetchone()[0]
        finally:
            con.close()
        if ok:
            return
        time.sleep(poll)
    raise TimeoutError(f"Condition never satisfied within {timeout}s: {condition_sql}")


def _wait_idle(db_path: Path, timeout: float = 60.0) -> None:
    """Block until no demand rows remain and no runs are still running."""
    _wait_for(
        db_path,
        "SELECT COUNT(*) = 0 FROM demand",
        timeout=timeout,
    )
    _wait_for(
        db_path,
        "SELECT COUNT(*) = 0 FROM pond_run WHERE status = 'running'",
        timeout=5.0,
    )


def _db(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(root / "duck.db"))


# ---------------------------------------------------------------------------
# Basic pipeline tests
# ---------------------------------------------------------------------------

def test_pulse_unknown_outlet_404(orch_catchment):
    url, _ = orch_catchment
    r = httpx.post(f"{url}/api/outlets/nonexistent/pulse", timeout=10.0)
    assert r.status_code == 404


def test_pulse_all_ponds_run(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    r = httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    assert r.status_code == 200

    _wait_idle(root / "duck.db")

    db = _db(root)
    successful = {
        row[0]
        for row in db.execute("""
            SELECT p.name FROM pond_run pr
            JOIN pond_version pv ON pv.id = pr.pond_version_id
            JOIN pond p ON p.id = pv.pond_id
            WHERE pr.status = 'success'
        """).fetchall()
    }
    db.close()
    assert {"transactions", "products", "sales", "reports"} <= successful


def test_pulse_outlet_gen_one(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    row = db.execute("""
        SELECT pr.generation FROM pond_run pr
        JOIN pond_version pv ON pv.id = pr.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = 'reports' AND pr.status = 'success'
        ORDER BY pr.generation
        LIMIT 1
    """).fetchone()
    db.close()
    assert row is not None
    assert row[0] == 1


def test_pulse_watermarks(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    wm_rows = db.execute("""
        SELECT sink.name, src.name, w.generation
        FROM watermark w
        JOIN pond sink ON sink.id = w.sink_pond_id
        JOIN pond src  ON src.id  = w.source_pond_id
    """).fetchall()
    db.close()

    wm = {(sink, source): gen for sink, source, gen in wm_rows}
    assert wm.get(("sales", "transactions"), 0) >= 1
    assert wm.get(("sales", "products"), 0) >= 1
    assert wm.get(("reports", "sales"), 0) >= 1


def test_pulse_outlet_data(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    import duckdb
    reg = duckdb.connect(str(root / "ponds" / "reports" / "registry.duckdb"))
    tables = {row[0] for row in reg.execute("SHOW TABLES").fetchall()}
    reg.close()
    assert "monthly_summary" in tables


# ---------------------------------------------------------------------------
# Stop mechanism tests
# ---------------------------------------------------------------------------

def test_pulse_all_ponds_stopped_after_completion(orch_catchment):
    """After a pulse completes, every pond should be in stopped state (is_stopped=1)."""
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    not_stopped = db.execute("""
        SELECT p.name FROM pond_version pv
        JOIN pond p ON p.id = pv.pond_id
        WHERE pv.is_active = 1 AND pv.is_stopped = 0
    """).fetchall()
    db.close()
    assert not_stopped == [], f"Ponds not stopped after pulse: {[r[0] for r in not_stopped]}"


def test_no_demand_after_pulse_completes(orch_catchment):
    """Demand and stop tables should both be empty after a completed pulse chain."""
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    demand_count = db.execute("SELECT COUNT(*) FROM demand").fetchone()[0]
    stop_count = db.execute("SELECT COUNT(*) FROM stop").fetchone()[0]
    db.close()
    assert demand_count == 0
    assert stop_count == 0


def test_pulse_twice_runs_twice(orch_catchment):
    """Two sequential pulses should each complete a full chain run (gen=2 for all ponds)."""
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    max_gen = {
        row[0]: row[1]
        for row in db.execute("""
            SELECT p.name, MAX(pr.generation)
            FROM pond_run pr
            JOIN pond_version pv ON pv.id = pr.pond_version_id
            JOIN pond p ON p.id = pv.pond_id
            WHERE pr.status = 'success'
            GROUP BY p.name
        """).fetchall()
    }
    db.close()
    for name in ("transactions", "products", "sales", "reports"):
        assert max_gen.get(name, 0) == 2, f"{name} expected gen=2, got {max_gen.get(name)}"


def test_pulse_does_not_run_again_without_new_pulse(orch_catchment):
    """After a pulse chain finishes, no further runs should start without another pulse."""
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/reports/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    run_count_after = db.execute(
        "SELECT COUNT(*) FROM pond_run WHERE status = 'success'"
    ).fetchone()[0]
    db.close()

    # Wait briefly to ensure no extra runs triggered spontaneously
    time.sleep(1.0)

    db = _db(root)
    run_count_later = db.execute(
        "SELECT COUNT(*) FROM pond_run WHERE status = 'success'"
    ).fetchone()[0]
    db.close()
    assert run_count_later == run_count_after, "Extra runs fired without a new pulse"


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------

def _make_broken_pond(base_dir: Path, name: str, version: str = "1.0.0") -> Path:
    """Create a minimal pond at base_dir/name with a syntax error in pond.py."""
    pond_dir = base_dir / name
    pond_dir.mkdir()
    (pond_dir / "pond.toml").write_text(
        f'[pond]\nname = "{name}"\nversion = "{version}"\ntype = "inlet"\n'
    )
    (pond_dir / "src").mkdir()
    (pond_dir / "src" / "pond.py").write_text(
        "from duckstring import ripple\n\n@ripple\ndef ingest(pond):\n    raise RuntimeError('intentional failure')\n"
    )
    return pond_dir


def _deploy_dir(url: str, pond_dir: Path, name: str, version: str, kind: str = "inlet") -> None:
    archive = _zip_dir(pond_dir)
    r = httpx.post(
        f"{url}/api/deploy",
        files={"pond": ("pond.zip", archive, "application/zip")},
        data={"name": name, "version": version, "type": kind},
        timeout=30.0,
    )
    assert r.status_code == 200, f"Deploy failed: {r.text}"


def _wait_blocked(db_path: Path, pond_name: str, timeout: float = 30.0) -> None:
    """Block until a pond is stopped with no demand (retries exhausted)."""
    _wait_for(
        db_path,
        f"""
        SELECT COUNT(*) FROM pond_version pv
        JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = '{pond_name}' AND pv.is_active = 1
          AND pv.is_stopped = 1
          AND NOT EXISTS (SELECT 1 FROM demand d WHERE d.pond_version_id = pv.id)
          AND NOT EXISTS (SELECT 1 FROM pond_run pr WHERE pr.pond_version_id = pv.id AND pr.status = 'running')
        """,
        timeout=timeout,
    )


def test_default_retries_zero_goes_silent_on_failure(orch_catchment, tmp_path):
    """With immediate_retries=0 (default), a failing inlet should go silent after one attempt."""
    url, root = orch_catchment

    pond_dir = _make_broken_pond(tmp_path, "bad_inlet")
    _deploy_dir(url, pond_dir, "bad_inlet", "1.0.0", "inlet")

    # Create a minimal outlet that depends on bad_inlet
    outlet_dir = tmp_path / "sink"
    outlet_dir.mkdir()
    (outlet_dir / "pond.toml").write_text(
        '[pond]\nname = "sink"\nversion = "1.0.0"\ntype = "outlet"\n\n[sources]\nbad_inlet = "1.0.0"\n'
    )
    (outlet_dir / "src").mkdir()
    (outlet_dir / "src" / "pond.py").write_text(
        "from duckstring import ripple\n\n@ripple\ndef read(pond):\n    pond.read_table('bad_inlet.ingest')\n"
    )
    _deploy_dir(url, outlet_dir, "sink", "1.0.0", "outlet")

    r = httpx.post(f"{url}/api/outlets/sink/pulse", timeout=10.0)
    assert r.status_code == 200

    _wait_blocked(root / "duck.db", "bad_inlet")

    db = _db(root)
    # Exactly one failed run for bad_inlet
    fail_count = db.execute("""
        SELECT COUNT(*) FROM pond_run pr
        JOIN pond_version pv ON pv.id = pr.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = 'bad_inlet' AND pr.status = 'failed'
    """).fetchone()[0]
    demand_count = db.execute("""
        SELECT COUNT(*) FROM demand d
        JOIN pond_version pv ON pv.id = d.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = 'bad_inlet'
    """).fetchone()[0]
    db.close()

    assert fail_count == 1, f"Expected 1 failed run, got {fail_count}"
    assert demand_count == 0, "Demand should be cleared after retry exhaustion"


def test_immediate_retries_allows_extra_attempts(orch_catchment, tmp_path):
    """With immediate_retries=2, a failing inlet should attempt 3 times before going silent."""
    url, root = orch_catchment

    pond_dir = tmp_path / "retry_inlet"
    pond_dir.mkdir()
    (pond_dir / "pond.toml").write_text(
        '[pond]\nname = "retry_inlet"\nversion = "1.0.0"\ntype = "inlet"\nimmediate_retries = 2\n'
    )
    (pond_dir / "src").mkdir()
    (pond_dir / "src" / "pond.py").write_text(
        "from duckstring import ripple\n\n@ripple\ndef ingest(pond):\n    raise RuntimeError('always fails')\n"
    )

    outlet_dir = tmp_path / "sink2"
    outlet_dir.mkdir()
    (outlet_dir / "pond.toml").write_text(
        '[pond]\nname = "sink2"\nversion = "1.0.0"\ntype = "outlet"\n\n[sources]\nretry_inlet = "1.0.0"\n'
    )
    (outlet_dir / "src").mkdir()
    (outlet_dir / "src" / "pond.py").write_text(
        "from duckstring import ripple\n\n@ripple\ndef read(pond):\n    pass\n"
    )

    _deploy_dir(url, pond_dir, "retry_inlet", "1.0.0", "inlet")
    _deploy_dir(url, outlet_dir, "sink2", "1.0.0", "outlet")

    httpx.post(f"{url}/api/outlets/sink2/pulse", timeout=10.0)
    _wait_blocked(root / "duck.db", "retry_inlet", timeout=60.0)

    db = _db(root)
    fail_count = db.execute("""
        SELECT COUNT(*) FROM pond_run pr
        JOIN pond_version pv ON pv.id = pr.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = 'retry_inlet' AND pr.status = 'failed'
    """).fetchone()[0]
    db.close()

    assert fail_count == 3, f"Expected 3 failed attempts (1 initial + 2 retries), got {fail_count}"
