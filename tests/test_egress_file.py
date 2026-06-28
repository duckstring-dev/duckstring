"""Egress execution: the egress-driver seam, the object-store (file://) driver, and the worker that
delivers a Pond's published output to a Spout. The slice's e2e is file:// snapshot delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import duckdb
import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.egress_worker import _drain, _egress_spout
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.registry import pond_data_dir
from duckstring.catchment.routes.deploy import _register
from duckstring.egress import DestinationError, get_egress
from duckstring.egress.base import Capabilities

pytestmark = pytest.mark.timeout(10)

UTC = timezone.utc
_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "outlet"}


@pytest.fixture(autouse=True)
def _parquet_plane(monkeypatch):
    # Pin the offline/flat data plane so the worker's read needs no iceberg extension download.
    monkeypatch.setenv("DUCKSTRING_DATA_PLANE", "parquet")


# ─── The seam ────────────────────────────────────────────────────────────────


def test_get_egress_resolves_file_driver():
    drv = get_egress("file:///tmp/out")
    caps = drv.capabilities()
    assert isinstance(caps, Capabilities)
    assert caps == Capabilities(supports_delta=False, supports_delete=False, transactional=False)


def test_get_egress_unimplemented_scheme_is_clear():
    with pytest.raises(DestinationError, match="not implemented yet"):
        get_egress("s3://bucket/prefix")
    with pytest.raises(DestinationError, match="not implemented yet"):
        get_egress("postgres://u@h/db")


def test_file_driver_write_full(tmp_path):
    drv = get_egress(f"file://{tmp_path / 'out'}")
    con = duckdb.connect()
    rel = con.sql("SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS t(id, name)")
    drv.write_full(rel, table="revenue", pk=["id"], f=datetime(2026, 1, 1, tzinfo=UTC))

    out = tmp_path / "out" / "revenue.parquet"
    assert out.exists()
    got = duckdb.connect().execute(f"SELECT id, name FROM read_parquet('{out}') ORDER BY id").fetchall()
    assert got == [(1, "a"), (2, "b")]


def test_file_driver_rejects_remote_file_host():
    drv = get_egress("file://remotehost/path")
    con = duckdb.connect()
    with pytest.raises(ValueError, match="absolute local path"):
        drv.write_full(con.sql("SELECT 1 AS id"), table="t", pk=None, f=datetime(2026, 1, 1, tzinfo=UTC))


# ─── The worker against a published Pond ─────────────────────────────────────


def _publish_table(root, name, major, table, sql):
    """Write a Pond's exported parquet directly (the snapshot the worker reads)."""
    data_dir = pond_data_dir(root, name, major)
    data_dir.mkdir(parents=True, exist_ok=True)
    duckdb.connect().execute(f"COPY ({sql}) TO '{data_dir / f'{table}.parquet'}' (FORMAT PARQUET)")


def _driver_with_published(tmp_path, end_f):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _CFG,
              [{"func": "f", "name": "agg", "parents": []}])
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    _publish_table(tmp_path, "sales", 1, "revenue", "SELECT * FROM (VALUES (1, 10), (2, 20)) t(id, amt)")
    d.state.pond_states["sales@1"].end_f = end_f  # simulate a completed run at this freshness
    return d


def test_worker_delivers_snapshot_and_advances_watermark(tmp_path):
    f1 = datetime(2026, 6, 1, tzinfo=UTC)
    d = _driver_with_published(tmp_path, f1)
    out = tmp_path / "egress"
    d.add_spout("sales@1", None, None, f"file://{out}", "full")

    jobs = d.egress_pending()
    assert len(jobs) == 1 and jobs[0]["spout"] == "file" and jobs[0]["table"] is None and jobs[0]["f"] == f1

    asyncio.run(_drain(d, tmp_path))

    assert (out / "revenue.parquet").exists()
    rows = duckdb.connect().execute(
        f"SELECT id, amt FROM read_parquet('{out / 'revenue.parquet'}') ORDER BY id"
    ).fetchall()
    assert rows == [(1, 10), (2, 20)]

    # Watermark advanced → nothing pending until the Pond publishes further.
    assert d.egress_pending() == []
    assert d.list_spouts("sales@1")[0]["watermark"] == f1.isoformat()


def test_worker_redelivers_when_pond_advances(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    out = tmp_path / "egress"
    d.add_spout("sales@1", None, None, f"file://{out}", "full")
    asyncio.run(_drain(d, tmp_path))
    assert d.egress_pending() == []

    # The Pond runs again with new data at a later freshness.
    _publish_table(tmp_path, "sales", 1, "revenue", "SELECT * FROM (VALUES (1, 99)) t(id, amt)")
    d.state.pond_states["sales@1"].end_f = datetime(2026, 6, 2, tzinfo=UTC)
    assert len(d.egress_pending()) == 1
    asyncio.run(_drain(d, tmp_path))
    rows = duckdb.connect().execute(f"SELECT amt FROM read_parquet('{out / 'revenue.parquet'}')").fetchall()
    assert rows == [(99,)]


def test_worker_failure_parks_spout_then_resync_rearms(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    # A known scheme with no driver yet → delivery fails; budget 0 → parks immediately.
    d.add_spout("sales@1", "lake", None, "s3://bucket/sales", "full")
    asyncio.run(_drain(d, tmp_path))

    s = next(x for x in d.list_spouts("sales@1") if x["name"] == "lake")
    assert s["is_failed"] is True and s["failures"] == 1 and "not implemented" in (s["error"] or "")
    assert d.egress_pending() == []  # parked → not retried

    assert d.resync_spout("sales@1", "lake") is True
    s = next(x for x in d.list_spouts("sales@1") if x["name"] == "lake")
    assert s["is_failed"] is False and s["failures"] == 0
    assert len(d.egress_pending()) == 1  # re-armed


def test_worker_retry_budget(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    d.add_spout("sales@1", "lake", None, "s3://bucket/sales", "full")
    d.db.execute("UPDATE pond_spout SET retries = 2 WHERE name = 'lake'")
    d.db.commit()

    for expected in (1, 2):  # within budget → stays unparked, still pending
        asyncio.run(_drain(d, tmp_path))
        s = next(x for x in d.list_spouts("sales@1") if x["name"] == "lake")
        assert s["failures"] == expected and s["is_failed"] is False
        assert len(d.egress_pending()) == 1

    asyncio.run(_drain(d, tmp_path))  # exhausts the budget (3rd failure > 2)
    s = next(x for x in d.list_spouts("sales@1") if x["name"] == "lake")
    assert s["failures"] == 3 and s["is_failed"] is True


@pytest.mark.timeout(60)
def test_egress_e2e_file_snapshot(tmp_path_factory, monkeypatch):
    """A real Catchment + Duck: deploy a Pond, add a file:// Spout, run it — the egress worker delivers
    the published table as Parquet after the run completes."""
    import io
    import socket
    import threading
    import time
    import zipfile

    import httpx
    import uvicorn

    from duckstring.catchment.app import create_app

    root = tmp_path_factory.mktemp("egress_e2e_root")
    out = tmp_path_factory.mktemp("egress_e2e_out") / "landed"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)  # real Ducks
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)

    server = uvicorn.Server(uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="error"))
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
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("pond.toml", '[pond]\nname = "sales"\nversion = "1.0.0"\ntype = "inlet"\n')
            zf.writestr(
                "src/pond.py",
                "from duckstring import ripple\n\n@ripple\ndef agg(pond):\n"
                "    pond.write_table('revenue', pond.con.sql('SELECT 1 AS id, 42 AS amt'))\n",
            )
        r = httpx.post(
            f"{url}/api/deploy",
            files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
            data={"name": "sales", "version": "1.0.0", "type": "inlet"}, timeout=15.0,
        )
        assert r.status_code == 200, r.text

        r = httpx.post(f"{url}/api/ponds/sales/spouts", json={"destination": f"file://{out}"}, timeout=5.0)
        assert r.status_code == 200, r.text

        httpx.post(f"{url}/api/ponds/sales/pulse", timeout=5.0)

        landed = out / "revenue.parquet"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and not landed.exists():
            time.sleep(0.25)
        assert landed.exists(), "the egress worker should land the Pond's table as Parquet"
        rows = duckdb.connect().execute(f"SELECT id, amt FROM read_parquet('{landed}')").fetchall()
        assert rows == [(1, 42)]
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_egress_spout_specific_table(tmp_path):
    _publish_table(tmp_path, "sales", 1, "revenue", "SELECT 1 AS id, 10 AS amt")
    _publish_table(tmp_path, "sales", 1, "other", "SELECT 7 AS id")
    out = tmp_path / "egress"
    job = {"pond_id": 1, "pond_name": "sales", "major": 1, "spout": "rev",
           "table": "revenue", "destination": f"file://{out}", "mode": "full",
           "f": datetime(2026, 6, 1, tzinfo=UTC)}
    _egress_spout(tmp_path, job)
    assert (out / "revenue.parquet").exists()
    assert not (out / "other.parquet").exists()  # only the named table
