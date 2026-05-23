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
    for name in ("inlet", "pond", "outlet"):
        _deploy_demo(url, name)


def _wait_idle(db_path: Path, timeout: float = 30.0) -> None:
    """Block until demand table is empty or timeout (in seconds)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        con = sqlite3.connect(str(db_path))
        try:
            count = con.execute("SELECT COUNT(*) FROM demand").fetchone()[0]
        finally:
            con.close()
        if count == 0:
            return
        time.sleep(0.1)
    raise TimeoutError(f"Demand table still non-empty after {timeout}s")


def _db(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(root / "duck.db"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_pulse_unknown_outlet_404(orch_catchment):
    url, _ = orch_catchment
    r = httpx.post(f"{url}/api/outlets/nonexistent/pulse", timeout=10.0)
    assert r.status_code == 404


def test_pulse_all_three_ponds_run(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    r = httpx.post(f"{url}/api/outlets/outlet/pulse", timeout=10.0)
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
    assert {"inlet", "pond", "outlet"} <= successful


def test_pulse_outlet_gen_one(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/outlet/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    row = db.execute("""
        SELECT pr.generation FROM pond_run pr
        JOIN pond_version pv ON pv.id = pr.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE p.name = 'outlet' AND pr.status = 'success'
        ORDER BY pr.generation
        LIMIT 1
    """).fetchone()
    db.close()
    assert row is not None
    assert row[0] == 1


def test_pulse_watermarks(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/outlet/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    db = _db(root)
    wm_rows = db.execute("""
        SELECT sink.name, src.name, w.source_major, w.generation
        FROM watermark w
        JOIN pond sink ON sink.id = w.sink_pond_id
        JOIN pond src  ON src.id  = w.source_pond_id
    """).fetchall()
    db.close()

    wm = {(sink, source): gen for sink, source, major, gen in wm_rows}
    assert wm.get(("pond", "inlet"), 0) >= 1
    assert wm.get(("outlet", "pond"), 0) >= 1


def test_pulse_outlet_data(orch_catchment):
    url, root = orch_catchment
    _deploy_all(url)

    httpx.post(f"{url}/api/outlets/outlet/pulse", timeout=10.0)
    _wait_idle(root / "duck.db")

    import duckdb
    reg = duckdb.connect(str(root / "ponds" / "outlet" / "registry.duckdb"))
    count = reg.execute('SELECT count(*) FROM "outlet"."daily"').fetchone()[0]
    reg.close()
    assert count == 10
