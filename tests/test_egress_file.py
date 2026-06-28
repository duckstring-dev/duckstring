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
    # An unwritable destination → delivery fails fast (mkdir under a file, no network); budget 0 → parks.
    d.add_spout("sales@1", "lake", None, "file:///dev/null/nope", "full")
    asyncio.run(_drain(d, tmp_path))

    s = next(x for x in d.list_spouts("sales@1") if x["name"] == "lake")
    assert s["is_failed"] is True and s["failures"] == 1 and s["error"]
    assert d.egress_pending() == []  # parked → not retried

    assert d.resync_spout("sales@1", "lake") is True
    s = next(x for x in d.list_spouts("sales@1") if x["name"] == "lake")
    assert s["is_failed"] is False and s["failures"] == 0
    assert len(d.egress_pending()) == 1  # re-armed


def test_worker_retry_budget(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    d.add_spout("sales@1", "lake", None, "file:///dev/null/nope", "full")
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


# ─── The standing Wake: Control verbs (sleep/wake/kill/force/clear) + running guard ──


def _spout(d, name="file"):
    return next(x for x in d.list_spouts("sales@1") if x["name"] == name)


def test_spout_sleep_disarms_wake_rearms(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    d.add_spout("sales@1", None, None, f"file://{tmp_path / 'o'}", "full")
    assert len(d.egress_pending()) == 1  # armed by default

    assert d.spout_sleep("sales@1", "file") is True
    assert _spout(d)["standing_wake"] is False
    assert d.egress_pending() == []  # disarmed → never fires

    assert d.spout_wake("sales@1", "file") is True
    assert _spout(d)["standing_wake"] is True
    assert len(d.egress_pending()) == 1  # re-armed


def test_spout_kill_parks_until_clear(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    d.add_spout("sales@1", None, None, f"file://{tmp_path / 'o'}", "full")
    d.spout_kill("sales@1", "file")
    assert _spout(d)["is_killed"] is True and _spout(d)["standing_wake"] is False
    assert d.egress_pending() == []
    d.spout_clear("sales@1", "file")
    assert _spout(d)["is_killed"] is False
    # clear leaves it disarmed (kill turned the wake off); wake re-arms.
    assert d.egress_pending() == []
    d.spout_wake("sales@1", "file")
    assert len(d.egress_pending()) == 1


def test_spout_force_redelivers_after_caught_up(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    out = tmp_path / "o"
    d.add_spout("sales@1", None, None, f"file://{out}", "full")
    asyncio.run(_drain(d, tmp_path))
    assert d.egress_pending() == []  # delivered, caught up

    d.spout_force("sales@1", "file")  # re-deliver the current freshness
    assert len(d.egress_pending()) == 1


def test_running_guard_excludes_in_flight_spout(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    d.add_spout("sales@1", None, None, f"file://{tmp_path / 'o'}", "full")
    pond_id = d.meta["sales@1"]["pond_id"]
    d.mark_egress_running(pond_id, "file", True)
    assert d.egress_pending() == []  # mid-delivery → not re-dispatched
    assert _spout(d)["running"] is True
    d.mark_egress_running(pond_id, "file", False)
    assert len(d.egress_pending()) == 1


def test_status_surfaces_spout_nodes(tmp_path):
    d = _driver_with_published(tmp_path, datetime(2026, 6, 1, tzinfo=UTC))
    dest = f"file://{tmp_path / 'o'}"
    d.add_spout("sales@1", "revenue", "revenue", dest, "auto")
    spouts = d.status()["spouts"]
    assert len(spouts) == 1
    s = spouts[0]
    assert s["id"] == "sales@1#revenue" and s["source"] == "sales@1"
    assert s["destination"] == dest and s["table"] == "revenue"
    assert s["status"] == "queued"  # source advanced past delivered → wants to deliver

    asyncio.run(_drain(d, tmp_path))
    assert d.status()["spouts"][0]["status"] == "delivered"  # caught up
    d.spout_sleep("sales@1", "revenue")
    assert d.status()["spouts"][0]["status"] == "asleep"


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
