"""Cross-Catchment ducts: Pond Draws (synthetic inlet nodes fed by a poller), the consumer-side
duct CRUD, the producer-side open/draw routes, and the poller end-to-end (status mirror → transfer →
completion → soliciting). See plans/cross-catchment-ducts.md.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timezone

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
    assert any(x["key"] == "sales@1" and x["wants_upstream"] for x in d.draws())

    d.observe_remote("sales@1", f)
    t = d.take_transfers()[0]
    d.complete_draw_transfer("sales@1", t["f"])
    # snk now has a fresher source and starts a run at that freshness.
    assert d.state.pond_states["snk@1"].start_f == f


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
