"""Concurrent major versions: two deployed majors of one Pond name are independent live Ponds.

Engine-level tests drive the Catchment app with Ducks disabled; the e2e test at the bottom runs real
Duck subprocesses and checks the per-major storage layout (`ponds/{name}/m{major}/`).
"""

from __future__ import annotations

import io
import socket
import threading
import time
import zipfile

import httpx
import pytest
import uvicorn

pytestmark = pytest.mark.timeout(10)


def _zip_files(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _deploy(client, name: str, version: str, kind: str = "inlet", sources: dict[str, str] | None = None,
            pond_py: str | None = None):
    toml = f'[pond]\nname = "{name}"\nversion = "{version}"\ntype = "{kind}"\n'
    if sources:
        toml += "\n[sources]\n" + "".join(f'{s} = "{v}"\n' for s, v in sources.items())
    files = {"pond.toml": toml}
    if pond_py is not None:
        files["src/pond.py"] = pond_py
    r = client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", _zip_files(files), "application/zip")},
        data={"name": name, "version": version, "type": kind},
    )
    assert r.status_code == 200, r.text
    return r


def _pond(client, pond_id: str) -> dict:
    ponds = client.get("/api/status").json()["ponds"]
    return next(p for p in ponds if p["id"] == pond_id)


def test_two_majors_are_independent_live_ponds(catchment_client):
    _deploy(catchment_client, "inlet", "1.0.0")
    _deploy(catchment_client, "inlet", "2.0.0")

    status = catchment_client.get("/api/status").json()
    ids = {p["id"] for p in status["ponds"]}
    assert {"inlet@1", "inlet@2"} <= ids
    assert _pond(catchment_client, "inlet@1")["version"] == "1.0.0"
    assert _pond(catchment_client, "inlet@2")["version"] == "2.0.0"


def test_trigger_targets_one_major_only(catchment_client):
    _deploy(catchment_client, "inlet", "1.0.0")
    _deploy(catchment_client, "inlet", "2.0.0")

    # Explicit major: only that line runs.
    catchment_client.post("/api/ponds/inlet/pulse", params={"major": 1})
    assert _pond(catchment_client, "inlet@1")["gen"] == 1
    assert _pond(catchment_client, "inlet@2")["gen"] == 0

    # Default: the highest deployed major.
    catchment_client.post("/api/ponds/inlet/pulse")
    assert _pond(catchment_client, "inlet@1")["gen"] == 1
    assert _pond(catchment_client, "inlet@2")["gen"] == 1


def test_version_param_requires_selected_version(catchment_client):
    _deploy(catchment_client, "inlet", "1.0.0")
    _deploy(catchment_client, "inlet", "1.1.0")  # 1.1.0 is now the selected version of major 1

    assert catchment_client.post("/api/ponds/inlet/pulse", params={"version": "1.1.0"}).status_code == 200
    r = catchment_client.post("/api/ponds/inlet/pulse", params={"version": "1.0.0"})
    assert r.status_code == 422
    assert "not the selected version" in r.json()["detail"]
    # Conflicting major + version is rejected; an undeployed major is 404.
    assert catchment_client.post("/api/ponds/inlet/pulse", params={"major": 2, "version": "1.1.0"}).status_code == 422
    assert catchment_client.post("/api/ponds/inlet/pulse", params={"major": 3}).status_code == 404
    assert catchment_client.post("/api/ponds/ghost/pulse").status_code == 404


def test_sink_wires_to_its_pinned_source_major(catchment_client):
    _deploy(catchment_client, "src", "1.0.0")
    _deploy(catchment_client, "src", "2.0.0")
    _deploy(catchment_client, "snk", "1.0.0", kind="pond", sources={"src": "1.0.0"})

    driver = catchment_client.app.state.driver
    assert driver.state.ponds["snk@1"].sources == ["src@1"]

    # A pulse on the sink solicits its pinned source line, not the newer major.
    catchment_client.post("/api/ponds/snk/pulse")
    assert _pond(catchment_client, "src@1")["gen"] == 1
    assert _pond(catchment_client, "src@2")["gen"] == 0


def test_run_history_filters_by_major(catchment_client):
    _deploy(catchment_client, "inlet", "1.0.0")
    _deploy(catchment_client, "inlet", "2.0.0")
    catchment_client.post("/api/ponds/inlet/pulse", params={"major": 1})
    catchment_client.post("/api/ponds/inlet/pulse", params={"major": 2})

    all_runs = catchment_client.get("/api/runs", params={"pond": "inlet"}).json()["runs"]
    assert {r["id"] for r in all_runs} == {"inlet@2"}  # default resolution: highest major
    m1 = catchment_client.get("/api/runs", params={"pond": "inlet", "major": 1}).json()["runs"]
    assert {(r["id"], r["version"]) for r in m1} == {("inlet@1", "1.0.0")}


# ─── End-to-end: real Ducks, per-major storage ──────────────────────────────────


_POND_PY = """\
from duckstring import ripple

@ripple
def make(pond):
    pond.write_table("event", pond.con.sql("SELECT {value} AS marker"))
"""


@pytest.mark.timeout(60)
def test_two_majors_execute_concurrently_e2e(tmp_path_factory, monkeypatch):
    from duckstring.catchment.app import create_app

    root = tmp_path_factory.mktemp("mm_root")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)  # real Ducks
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

    try:
        for version, value in (("1.0.0", 1), ("2.0.0", 2)):
            toml = f'[pond]\nname = "inlet"\nversion = "{version}"\ntype = "inlet"\n'
            files = {"pond.toml": toml, "src/pond.py": _POND_PY.format(value=value)}
            r = httpx.post(
                f"{url}/api/deploy",
                files={"pond": ("pond.zip", _zip_files(files), "application/zip")},
                data={"name": "inlet", "version": version, "type": "inlet"},
                timeout=15.0,
            )
            assert r.status_code == 200, r.text

        httpx.post(f"{url}/api/ponds/inlet/pulse", params={"major": 1}, timeout=5.0)
        httpx.post(f"{url}/api/ponds/inlet/pulse", params={"major": 2}, timeout=5.0)

        def fresh(pond_id: str) -> bool:
            ponds = httpx.get(f"{url}/api/status", timeout=5.0).json()["ponds"]
            p = next((x for x in ponds if x["id"] == pond_id), None)
            return p is not None and p.get("end_f") is not None

        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not (fresh("inlet@1") and fresh("inlet@2")):
            time.sleep(0.25)
        assert fresh("inlet@1") and fresh("inlet@2"), "both majors should complete a run"

        # Per-major storage: each line exported its own data and kept its own ledger.
        for major, value in ((1, 1), (2, 2)):
            data = root / "ponds" / "inlet" / f"m{major}" / "data" / "event.parquet"
            assert data.exists(), f"no exported data for major {major}"
            assert (root / "ponds" / "inlet" / f"m{major}" / "pond.db").exists()
            r = httpx.post(f"{url}/api/query", json={"pond": "inlet", "major": major, "ripple": "event"}, timeout=5.0)
            assert r.json()[0]["marker"] == value, f"major {major} data should be its own"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
