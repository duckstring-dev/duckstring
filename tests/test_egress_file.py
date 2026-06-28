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


def test_get_egress_rejects_unknown_scheme_and_resolves_known():
    with pytest.raises(DestinationError, match="unsupported destination scheme"):
        get_egress("ftp://host/path")
    for uri in ("file:///tmp/x", "s3://bucket/p", "gs://bucket/p", "postgres://u@h/db"):
        assert get_egress(uri) is not None  # every bundled scheme resolves to a driver


def test_file_driver_write_full(tmp_path):
    drv = get_egress(f"file://{tmp_path / 'out'}")
    con = duckdb.connect()
    rel = con.sql("SELECT * FROM (VALUES (1, 'a'), (2, 'b')) AS t(id, name)")
    drv.write_full(con, rel, table="revenue", pk=["id"], f=datetime(2026, 1, 1, tzinfo=UTC))

    out = tmp_path / "out" / "revenue.parquet"
    assert out.exists()
    got = duckdb.connect().execute(f"SELECT id, name FROM read_parquet('{out}') ORDER BY id").fetchall()
    assert got == [(1, "a"), (2, "b")]


def test_file_driver_rejects_remote_file_host():
    drv = get_egress("file://remotehost/path")
    con = duckdb.connect()
    with pytest.raises(ValueError, match="absolute local path"):
        drv.write_full(con, con.sql("SELECT 1 AS id"), table="t", pk=None, f=datetime(2026, 1, 1, tzinfo=UTC))


# ─── s3:// / gs:// target + secret construction (no network) ──────────────────


def test_s3_remote_target_strips_slashes_and_omits_query():
    drv = get_egress("s3://bucket/data/sales/?region=us-east-1&key_id=AK&secret=SK")
    target = drv._remote_target("revenue")
    assert target == "s3://bucket/data/sales/revenue.parquet"
    assert "key_id" not in target and "secret" not in target  # creds never travel in the target URI


def test_s3_remote_target_no_prefix():
    assert get_egress("s3://bucket")._remote_target("t") == "s3://bucket/t.parquet"


def test_s3_secret_sql_with_explicit_credentials():
    drv = get_egress("s3://b/p?key_id=AKIA&secret=shh&region=eu-west-1&url_style=path&use_ssl=false")
    sql = drv._secret_sql()
    assert sql.startswith("CREATE OR REPLACE SECRET __duckstring_egress (TYPE s3")
    for frag in ("KEY_ID 'AKIA'", "SECRET 'shh'", "REGION 'eu-west-1'", "URL_STYLE 'path'", "USE_SSL false"):
        assert frag in sql
    assert "credential_chain" not in sql


def test_s3_secret_sql_falls_back_to_credential_chain():
    sql = get_egress("s3://bucket/prefix")._secret_sql()
    assert "PROVIDER credential_chain" in sql and "KEY_ID" not in sql


def test_gs_secret_sql_requires_hmac_credentials():
    assert "TYPE gcs" in get_egress("gs://b/p?key_id=GK&secret=GS")._secret_sql()
    with pytest.raises(ValueError, match="HMAC credentials"):
        get_egress("gs://bucket/prefix")._secret_sql()


def test_remote_credentials_resolved_from_env_at_egress_time(monkeypatch):
    monkeypatch.setenv("AWS_KEY", "AKIARESOLVED")
    monkeypatch.setenv("AWS_SECRET", "topsecret")
    drv = get_egress("s3://bucket/out?key_id=${env:AWS_KEY}&secret=${env:AWS_SECRET}")
    sql = drv._secret_sql()
    assert "KEY_ID 'AKIARESOLVED'" in sql and "SECRET 'topsecret'" in sql
    # The stored destination still holds only the reference, never the resolved value.
    assert "${env:AWS_KEY}" in drv.dest.raw


def test_remote_missing_env_credential_surfaces_clearly():
    from duckstring.egress import CredentialError

    drv = get_egress("s3://bucket/out?key_id=${env:NOPE_UNSET}&secret=x")
    with pytest.raises(CredentialError, match="NOPE_UNSET"):
        drv._secret_sql()


def test_sql_quote_escapes_embedded_quote():
    sql = get_egress("s3://b/p?key_id=a'b&secret=x")._secret_sql()
    assert "KEY_ID 'a''b'" in sql  # doubled to neutralise injection


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


def _deliver(d, root):
    """Dispatch the Spout's run (the engine arms the standing Wake on the Source advance) and let the
    worker deliver + report completion — the real-node path."""
    d.scheduler_tick()
    asyncio.run(_drain(d, root))


def _would_dispatch(d, spout_key="sales#file@1"):
    """Whether the engine would dispatch this Spout now — non-mutating (arms a clone, checks the gate)."""
    from duckstring.catchment.driver import _now
    from duckstring.engine.catchment import can_start_pond, tick
    now = _now()
    return can_start_pond(tick(now, d.state.clone()), spout_key, now)


def _with_spout(tmp_path, dest, *, table=None, name=None, mode="full", end_f=None):
    end_f = end_f or datetime(2026, 6, 1, tzinfo=UTC)
    d = _driver_with_published(tmp_path, end_f)             # publishes the source parquet
    spout = d.add_spout("sales@1", name, table, dest, mode)  # add_spout reloads…
    d.state.pond_states["sales@1"].end_f = end_f             # …so re-assert the Source freshness
    return d, spout


def _sp(d, name):
    return next(x for x in d.list_spouts("sales@1") if x["name"] == name)


def test_worker_delivers_snapshot_and_records_history(tmp_path):
    out = tmp_path / "egress"
    d, _ = _with_spout(tmp_path, f"file://{out}")
    _deliver(d, tmp_path)

    assert (out / "revenue.parquet").exists()
    rows = duckdb.connect().execute(
        f"SELECT id, amt FROM read_parquet('{out / 'revenue.parquet'}') ORDER BY id"
    ).fetchall()
    assert rows == [(1, 10), (2, 20)]

    sp = _sp(d, "file")
    assert sp["watermark"] == datetime(2026, 6, 1, tzinfo=UTC).isoformat() and not sp["running"]
    runs = d.run_history("sales#file@1", lineage=False, ripples=False, limit=10)
    assert len(runs) == 1 and runs[0]["status"] == "success"
    assert not _would_dispatch(d)


def test_worker_redelivers_when_source_advances(tmp_path):
    out = tmp_path / "egress"
    d, _ = _with_spout(tmp_path, f"file://{out}")
    _deliver(d, tmp_path)
    assert not _would_dispatch(d)

    _publish_table(tmp_path, "sales", 1, "revenue", "SELECT * FROM (VALUES (1, 99)) t(id, amt)")
    d.state.pond_states["sales@1"].end_f = datetime(2026, 6, 2, tzinfo=UTC)
    _deliver(d, tmp_path)
    rows = duckdb.connect().execute(f"SELECT amt FROM read_parquet('{out / 'revenue.parquet'}')").fetchall()
    assert rows == [(99,)]


def test_worker_failure_records_failed_run_with_traceback(tmp_path):
    d, _ = _with_spout(tmp_path, "file:///dev/null/nope", name="lake")
    _deliver(d, tmp_path)

    sp = _sp(d, "lake")
    assert sp["is_failed"] is True and sp["error"]
    # PARITY: a Spout failure is a real failed pond_run with a traceback, in run history.
    runs = d.run_history("sales#lake@1", lineage=False, ripples=True, limit=10)
    assert runs[0]["status"] == "failed" and runs[0]["traceback"]

    assert d.spout_clear("sales@1", "lake") is True
    assert _sp(d, "lake")["is_failed"] is False
    assert _would_dispatch(d, "sales#lake@1")


# ─── The standing Wake: Control verbs (sleep/wake/kill/force/clear) ────────────


def test_spout_sleep_disarms_wake_rearms(tmp_path):
    d, _ = _with_spout(tmp_path, f"file://{tmp_path / 'o'}")
    assert _would_dispatch(d)  # armed by default

    assert d.spout_sleep("sales@1", "file") is True
    assert _sp(d, "file")["standing_wake"] is False
    assert not _would_dispatch(d)    # disarmed → never dispatches

    assert d.spout_wake("sales@1", "file") is True
    assert _sp(d, "file")["standing_wake"] is True
    assert _would_dispatch(d)        # re-armed


def test_spout_kill_parks_until_wake(tmp_path):
    d, _ = _with_spout(tmp_path, f"file://{tmp_path / 'o'}")
    d.spout_kill("sales@1", "file")
    assert _sp(d, "file")["is_killed"] is True and _sp(d, "file")["standing_wake"] is False
    assert not _would_dispatch(d)
    d.spout_clear("sales@1", "file")
    assert _sp(d, "file")["is_killed"] is False
    assert not _would_dispatch(d)    # clear leaves it disarmed (kill turned the Wake off)
    d.spout_wake("sales@1", "file")
    assert _would_dispatch(d)


def test_spout_force_redelivers_after_caught_up(tmp_path):
    d, _ = _with_spout(tmp_path, f"file://{tmp_path / 'o'}")
    _deliver(d, tmp_path)
    assert not _would_dispatch(d)    # delivered, caught up

    d.spout_force("sales@1", "file")  # re-deliver the current state
    assert _would_dispatch(d)


def test_status_surfaces_spout_as_node(tmp_path):
    dest = f"file://{tmp_path / 'o'}"
    d, _ = _with_spout(tmp_path, dest, table="revenue", name="revenue")
    spouts = [p for p in d.status()["ponds"] if p.get("is_spout")]
    assert len(spouts) == 1 and spouts[0]["id"] == "sales#revenue@1"
    assert ["sales@1", "sales#revenue@1"] in d.status()["edges"]

    _deliver(d, tmp_path)
    node = next(p for p in d.status()["ponds"] if p.get("is_spout"))
    assert node["status"] == "idle"  # delivered, caught up


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
