"""Cross-Catchment ducts: Pond Draws (synthetic inlet nodes fed by a poller), the consumer-side
duct CRUD, the producer-side open/draw routes, and the poller end-to-end (status mirror → transfer →
completion → soliciting). See plans/cross-catchment-ducts.md.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.poller import poll_once
from duckstring.catchment.registry import pond_data_dir
from duckstring.catchment.routes import router
from duckstring.catchment.routes.deploy import _register
from duckstring.engine import NEVER

pytestmark = pytest.mark.timeout(5)

_RIPPLES = [{"func": "f1", "name": "r1", "parents": []}]


def _cfg(sources=None, kind="inlet"):
    return {"sources": sources or {}, "immediate_retries": 0, "source_retries": 0, "kind": kind}


def _driver(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    return Driver(db, tmp_path, "http://x", NoopLauncher())


def _now():
    return datetime.now(timezone.utc)


# ─── Draw materialisation + lifecycle (driver level) ───────────────────────────


def test_add_duct_pond_materialises_a_draw(tmp_path):
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", {"Authorization": "Bearer x"})
    d.add_duct_pond("up", "sales", 1)

    assert "sales@1" in d.state.ponds
    assert d.state.ponds["sales@1"].is_draw
    assert "sales@1.draw" in d.state.ripple_states  # the transfer ripple
    assert d.list_ducts() == [
        {"origin": "up", "remote_url": "http://up", "ponds": [{"pond": "sales", "major": 1, "incremental": False}]}
    ]
    # Auth is never returned to a client, but the poller can resolve it.
    assert d.duct_targets()[0]["auth"] == {"Authorization": "Bearer x"}


def test_draw_transfers_on_demand_and_advances_freshness(tmp_path):
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    f = _now()

    # No demand yet: a fresh upstream alone does not trigger a transfer.
    d.observe_remote("sales@1", f)
    assert d.take_transfers() == []

    # Demand + fresh upstream → a transfer is queued, and the draw ripple shows running.
    d.tap("sales@1")
    d.observe_remote("sales@1", f)
    transfers = d.take_transfers()
    assert [t["key"] for t in transfers] == ["sales@1"]
    assert d.state.ripple_states["sales@1.draw"].is_running

    # Completion advances the draw's freshness to the upstream freshness.
    d.complete_draw_transfer("sales@1", transfers[0]["f"])
    assert d.state.pond_states["sales@1"].end_f == f
    assert not d.state.ripple_states["sales@1.draw"].is_running


def test_unreachable_upstream_blocks_the_draw(tmp_path):
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)

    d.observe_remote("sales@1", None, down=True)
    assert d.state.pond_states["sales@1"].is_blocked
    d.observe_remote("sales@1", _now(), down=False)
    assert not d.state.pond_states["sales@1"].is_blocked


def test_draw_freshness_cascades_to_a_local_sink(tmp_path):
    """A local Pond that depends on a drawn upstream sees its source go fresh and runs."""
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "snk", "1.0.0", "pond", "ponds/snk/1.0.0", _cfg(sources={"sales": "1.0.0"}), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)  # wires snk → sales@1

    assert d.state.ponds["snk@1"].sources == ["sales@1"]

    f = _now()
    d.tap("snk@1")  # demand cascades up to the draw (solicit)
    assert any(x["key"] == "sales@1" and (x["pull_m"] or x["target"]) for x in d.draws())

    d.observe_remote("sales@1", f)
    t = d.take_transfers()[0]
    d.complete_draw_transfer("sales@1", t["f"])
    # snk now has a fresher source and starts a run at that freshness.
    assert d.state.pond_states["snk@1"].start_f == f


def test_pending_pull_epoch_survives_reload(tmp_path):
    # A pull-driven Draw waiting on its upstream holds pull_m as its solicitation epoch; it must
    # survive a Catchment restart (persisted on pond_state), or the draw would stop soliciting.
    db = connect(tmp_path / "duck.db")
    migrate(db)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    d.tap("sales@1")  # pending pull (upstream not yet fresh) → pull_m minted
    pull_m = d.state.pond_states["sales@1"].pull_m
    assert pull_m != NEVER

    d.reload()  # simulate a restart: rebuild engine state from the DB
    assert d.state.pond_states["sales@1"].pull_m == pull_m
    assert d.draws()[0]["pull_m"] == pull_m.isoformat()  # still solicits with the original epoch


def test_remove_and_destroy_clean_up_draw_rows(tmp_path):
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    assert d.remove_duct_pond("up", "sales", 1)
    assert "sales@1" not in d.state.ponds
    assert d.destroy_duct("up")
    assert d.list_ducts() == []


def test_cannot_draw_over_an_existing_local_pond(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "inlet", "ponds/sales/1.0.0", _cfg(), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    d.create_duct("up", "http://up", None)
    with pytest.raises(ValueError, match="already exists"):
        d.add_duct_pond("up", "sales", 1)


# ─── Missing-source blocking (a declared Source absent from the Catchment) ─────


def _demo_minus_products(tmp_path):
    """transactions + products → sales → reports, with products NOT deployed locally."""
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "transactions", "1.0.0", "inlet", "ponds/transactions/1.0.0", _cfg(), _RIPPLES)
    _register(db, "sales", "1.0.0", "pond", "ponds/sales/1.0.0",
              _cfg(sources={"transactions": "1.0.0", "products": "1.0.0"}), _RIPPLES)
    _register(db, "reports", "1.0.0", "outlet", "ponds/reports/1.0.0",
              _cfg(sources={"sales": "1.0.0"}), _RIPPLES)
    return Driver(db, tmp_path, "http://x", NoopLauncher())


def test_missing_source_blocks_pond_and_downstream(tmp_path):
    d = _demo_minus_products(tmp_path)
    assert d.state.ponds["sales@1"].has_missing_source
    assert d.state.pond_states["sales@1"].is_blocked       # products absent
    assert d.state.pond_states["reports@1"].is_blocked      # blocked via its required Source sales


def test_pulse_does_not_run_a_pond_with_a_missing_source(tmp_path):
    d = _demo_minus_products(tmp_path)
    d.pulse("reports@1")  # the scenario: pulse the set before products is available
    # Nothing downstream of the gap runs, and the push does not cascade through sales to transactions.
    assert d.state.pond_states["sales@1"].start_f == NEVER
    assert d.state.pond_states["transactions@1"].start_f == NEVER
    assert d.state.pond_states["sales@1"].targets == []


def test_drawing_the_missing_source_unblocks(tmp_path):
    d = _demo_minus_products(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "products", 1)  # products now present (as a Draw) → reload re-derives
    assert not d.state.ponds["sales@1"].has_missing_source
    assert not d.state.pond_states["sales@1"].is_blocked
    assert not d.state.pond_states["reports@1"].is_blocked


def test_status_exposes_block_reason_and_failure_message(tmp_path):
    d = _demo_minus_products(tmp_path)
    by_id = {p["id"]: p for p in d.status()["ponds"]}
    # The Pond with the absent Source reports it (with version line); the downstream reports the cause.
    assert by_id["sales@1"]["status"] == "blocked"
    assert by_id["sales@1"]["missing_sources"] == ["products@1"]
    assert by_id["reports@1"]["status"] == "blocked"
    assert by_id["reports@1"]["blocked_by"] == ["sales@1"]


def test_status_exposes_failure_message(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "src", "1.0.0", "inlet", "ponds/src/1.0.0", _cfg(), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    d.tap("src@1")  # start a run so there is something in flight to fail
    d.on_event("src@1", {"kind": "pond_failed", "error": "boom: ledger write failed"})
    entry = next(p for p in d.status()["ponds"] if p["id"] == "src@1")
    assert entry["status"] == "failed"
    assert entry["error"] == "boom: ledger write failed"


def test_optional_missing_source_also_blocks(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    # "products?" is an optional source — still blocks while absent (every Source must be present).
    _register(db, "sales", "1.0.0", "pond", "ponds/sales/1.0.0",
              _cfg(sources={"products": "1.0.0?"}), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    assert d.state.ponds["sales@1"].has_missing_source
    assert d.state.pond_states["sales@1"].is_blocked


# ─── HTTP routes ───────────────────────────────────────────────────────────────


def _client(driver):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.driver = driver
    app.state.root = driver.root
    app.state.db = driver.db
    return TestClient(app)


def test_open_close_and_tap_on_get(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "inlet", "ponds/sales/1.0.0", _cfg(), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    client = _client(d)

    assert client.post("/api/ponds/sales/open", json={"tap_on_get": True}).json() == {"ok": True}
    assert d.pond_tap_on_get("sales@1")
    assert client.post("/api/ponds/sales/close").json() == {"ok": True}
    assert not d.pond_tap_on_get("sales@1")


def test_duct_crud_routes(tmp_path):
    client = _client(_driver(tmp_path))
    assert client.post("/api/duct", json={"origin": "up", "remote_url": "http://up"}).json() == {"ok": True}
    assert client.post("/api/duct/up/ponds", json={"pond": "sales", "major": 1}).json() == {"ok": True}
    ducts = client.get("/api/duct").json()["ducts"]
    assert ducts[0]["origin"] == "up" and ducts[0]["ponds"][0]["pond"] == "sales"
    assert "auth" not in ducts[0]  # creds never leave the server
    assert client.delete("/api/duct/up/ponds/sales", params={"major": 1}).json() == {"ok": True}
    assert client.delete("/api/duct/up").json() == {"ok": True}
    assert client.delete("/api/duct/up").status_code == 404


def test_draw_route_streams_all_parquet(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    data_dir = pond_data_dir(tmp_path, "sales", 1)
    data_dir.mkdir(parents=True)
    (data_dir / "orders.parquet").write_bytes(b"ORDERS")
    (data_dir / "items.parquet").write_bytes(b"ITEMS")

    resp = _client(d).get("/api/draw/sales/1")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert sorted(zf.namelist()) == ["items.parquet", "orders.parquet"]
        assert zf.read("orders.parquet") == b"ORDERS"


# ─── Poller end-to-end (mocked upstream transport) ─────────────────────────────


def _mock_upstream(f_iso: str, *, status="idle", tapped: list | None = None):
    """An httpx transport standing in for the upstream Catchment."""
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("orders.parquet", b"PARQUET-BYTES")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/status":
            return httpx.Response(200, json={"ponds": [
                {"name": "sales", "major": 1, "end_f": f_iso, "status": status}
            ], "edges": []})
        if path == "/api/draw/sales/1":
            return httpx.Response(200, content=zbuf.getvalue(), headers={"content-type": "application/zip"})
        if path == "/api/ponds/sales/tap":
            if tapped is not None:
                tapped.append(request.url.params.get("major"))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_poller_mirrors_fetches_and_lands_parquet(tmp_path):
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    d.tap("sales@1")  # downstream demand
    f = _now()

    transport = _mock_upstream(f.isoformat())
    client = httpx.AsyncClient(transport=transport)
    asyncio.run(poll_once(d, tmp_path, client))
    asyncio.run(client.aclose())

    # Freshness mirrored, transfer landed, draw advanced.
    landed = pond_data_dir(tmp_path, "sales", 1) / "orders.parquet"
    assert landed.read_bytes() == b"PARQUET-BYTES"
    assert d.state.pond_states["sales@1"].end_f == f


def test_poller_solicits_upstream_when_demand_unmet(tmp_path):
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    d.tap("sales@1")  # demand, but the upstream has nothing fresher yet

    tapped: list = []
    transport = _mock_upstream(datetime.min.replace(tzinfo=timezone.utc).isoformat(), tapped=tapped)
    client = httpx.AsyncClient(transport=transport)
    asyncio.run(poll_once(d, tmp_path, client))
    asyncio.run(client.aclose())

    assert tapped == ["1"]  # forwarded a Tap upstream for major 1


def test_poller_forwards_push_demand_with_its_epoch(tmp_path):
    # A push target at epoch T on the draw is forwarded upstream as a pulse carrying `at=T`, so the
    # upstream Inlet mints the same freshness (minted-freshness across the duct).
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    T = _now()
    d.pulse("sales@1", at=T)

    seen: dict = {}

    def handler(request):
        if request.url.path == "/api/status":  # present upstream, but nothing fresher yet
            return httpx.Response(200, json={
                "ponds": [{"name": "sales", "major": 1, "end_f": None, "status": "idle"}], "edges": []})
        if request.url.path == "/api/ponds/sales/pulse":
            seen["at"] = request.url.params.get("at")
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(poll_once(d, tmp_path, client))
    asyncio.run(client.aclose())
    assert seen["at"] == T.isoformat()


def test_pulse_at_makes_an_inlet_stamp_the_forwarded_epoch(tmp_path):
    # The producer side of the duct: a pulse carrying `at=T` makes the Inlet stamp T, not now.
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "transactions", "1.0.0", "inlet", "ponds/transactions/1.0.0", _cfg(), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    client = _client(d)

    T = _now() - timedelta(seconds=5)  # an earlier demand epoch
    assert client.post("/api/ponds/transactions/pulse", params={"at": T.isoformat()}).json() == {"ok": True}
    assert d.state.pond_states["transactions@1"].start_f == T


def test_poller_blocks_draw_when_upstream_unreachable(tmp_path):
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)

    def handler(request):
        raise httpx.ConnectError("unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(poll_once(d, tmp_path, client))
    asyncio.run(client.aclose())
    assert d.state.pond_states["sales@1"].is_blocked
