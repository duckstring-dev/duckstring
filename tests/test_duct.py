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

from duckstring.catchment.db import connect, ensure_identity, migrate
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


# ─── Catchment identity ────────────────────────────────────────────────────────


def test_identity_minted_once_and_name_refreshes(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    ensure_identity(db, "main")
    id1 = db.execute("SELECT value FROM catchment_meta WHERE key = 'id'").fetchone()[0]
    ensure_identity(db, "renamed")  # id is stable; name updates
    id2 = db.execute("SELECT value FROM catchment_meta WHERE key = 'id'").fetchone()[0]
    assert id1 and id1 == id2

    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    assert d.identity() == {"id": id1, "name": "renamed"}
    assert d.status()["catchment"] == {"id": id1, "name": "renamed"}


def test_identity_route_and_upstream_id_recorded(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    ensure_identity(db, "consumer")
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    assert _client(d).get("/api/catchment/identity").json()["name"] == "consumer"

    d.create_duct("up", "http://up", None, upstream_id="upstream-uuid")
    assert db.execute("SELECT upstream_id FROM duct WHERE origin_catchment = 'up'").fetchone()[0] == "upstream-uuid"


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


def test_draw_wait_long_poll(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "inlet", "ponds/sales/1.0.0", _cfg(), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    client = _client(d)

    # Not fresh yet → blocks until the (short) timeout, then returns the current observation.
    assert client.get("/api/draw/sales/1/wait", params={"timeout": 0.3}).json() == {"end_f": None, "down": False}

    # Freshness advanced past `after` → returns immediately.
    f = _now()
    d.state.pond_states["sales@1"].end_f = f
    body = client.get("/api/draw/sales/1/wait", params={"timeout": 5}).json()
    assert body["end_f"] == f.isoformat() and body["down"] is False

    # A down Pond returns at once (so a Draw learns of the fault without waiting) — it's a *transition*
    # from the consumer's last-known down=False.
    d.state.pond_states["sales@1"].is_failed = True
    assert client.get("/api/draw/sales/1/wait", params={"timeout": 5, "after": f.isoformat()}).json()["down"]

    # But once the consumer already knows it's down (down=True), a persistent down does NOT return
    # immediately — it holds until the timeout. This is what stops the poller spinning on a durably
    # blocked upstream.
    assert client.get(
        "/api/draw/sales/1/wait", params={"timeout": 0.3, "after": f.isoformat(), "down": True}
    ).json()["down"]


def test_notify_fires_on_demand_not_on_poller_observe(tmp_path):
    # The wake fires when downstream demand arrives (solicit promptly) but NOT from the poller's own
    # observe — otherwise the poller would wake itself in a tight loop.
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    calls: list = []
    d.set_notify(lambda: calls.append(1))

    d.tap("sales@1")          # demand → wake
    assert len(calls) >= 1
    n = len(calls)
    d.observe_remote("sales@1", _now())  # poller-driven → must NOT wake
    assert len(calls) == n


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


def test_draw_route_includes_trickle_sidecar(tmp_path):
    # A Trickle source's mode/PK sidecar must travel with the data — the consuming Catchment has no
    # access to the producer's duck.db, so read_delta resolves the source from this file.
    from duckstring.trickle_io import SIDECAR

    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _cfg(), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    data_dir = pond_data_dir(tmp_path, "sales", 1)
    data_dir.mkdir(parents=True)
    (data_dir / "order_line.parquet").write_bytes(b"DATA")
    (data_dir / "order_line__changelog.parquet").write_bytes(b"CDC")
    (data_dir / SIDECAR).write_text('{"order_line": {"mode": "merge", "pk": ["order_id"]}}')

    resp = _client(d).get("/api/draw/sales/1")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert SIDECAR in names
        assert "order_line.parquet" in names and "order_line__changelog.parquet" in names


# ─── Recursive lineage view ────────────────────────────────────────────────────


def test_cyclic_ducts_view_terminates(tmp_path):
    # downstream ⇄ upstream mutual ducts (the A↔B compute split). The visited-set must cut the cycle.
    from duckstring.catchment.routes.view import assemble_view

    def mk(name, ponds):
        db = connect(tmp_path / f"{name}.db")
        migrate(db)
        ensure_identity(db, name)
        for pname, kind, srcs in ponds:
            _register(db, pname, "1.0.0", kind, f"ponds/{pname}/1.0.0", _cfg(sources=srcs, kind=kind), _RIPPLES)
        (tmp_path / name).mkdir(exist_ok=True)
        return Driver(db, tmp_path / name, f"http://{name}", NoopLauncher())

    ds = mk("downstream", [("transactions", "inlet", {}), ("reports", "outlet", {"sales": "1.0.0"})])
    us = mk("upstream", [("products", "inlet", {}), ("sales", "pond", {"transactions": "1.0.0", "products": "1.0.0"})])
    ds_id, us_id = ds.identity()["id"], us.identity()["id"]
    ds.create_duct("upstream", "http://upstream", None, upstream_id=us_id)
    ds.add_duct_pond("upstream", "products", 1)
    ds.add_duct_pond("upstream", "sales", 1)
    us.create_duct("downstream", "http://downstream", None, upstream_id=ds_id)
    us.add_duct_pond("downstream", "transactions", 1)
    us.add_duct_pond("downstream", "reports", 1)

    by_url = {"http://upstream": us, "http://downstream": ds}

    class _Resp:
        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    class _Client:  # routes a /api/view fetch to the target Catchment's assemble_view, recursively
        async def get(self, url, params=None, headers=None, timeout=None):
            target = by_url[url.split("/api/view")[0]]
            scope = (params or {}).get("scope")
            visited = (params or {}).get("visited")
            scope_list = [s for s in scope.split(",") if s] if scope else None
            visited_set = {v for v in visited.split(",") if v} if visited else set()
            return _Resp(await assemble_view(target, scope_list, visited_set, self))

    result = asyncio.run(assemble_view(ds, None, set(), _Client()))
    assert {c["id"] for c in result["catchments"]} == {ds_id, us_id}  # both, once — cycle cut


# ─── Recursive lineage view ────────────────────────────────────────────────────


def _view_consumer(tmp_path, upstream_id="A-uuid"):
    """A consumer 'B' with a local sink drawing 'sales' from upstream 'A' (id=upstream_id)."""
    db = connect(tmp_path / "duck.db")
    migrate(db)
    ensure_identity(db, "B")
    _register(db, "snk", "1.0.0", "outlet", "ponds/snk/1.0.0", _cfg(sources={"sales": "1.0.0"}), _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    d.create_duct("A", "http://a", None, upstream_id=upstream_id)
    d.add_duct_pond("A", "sales", 1)  # materialises the sales@1 Draw, wires snk -> sales@1
    return d


def test_view_fragment_scopes_to_ancestors(tmp_path):
    d = _view_consumer(tmp_path)
    frag = d.view_fragment(["snk@1"])  # snk + its ancestor (the sales draw)
    ids = {p["id"] for p in frag["ponds"]}
    assert ids == {"snk@1", "sales@1"}
    assert frag["ducts"] and frag["ducts"][0]["drawn"] == ["sales@1"]


def test_assemble_view_recurses_merges_and_emits_boundary_edge(tmp_path):
    from duckstring.catchment.routes.view import assemble_view

    d = _view_consumer(tmp_path)
    # Mock A's /api/view: A's own pond 'sales' plus a 'C' it draws (transitive — C shows through).
    a_response = {
        "catchments": [
            {"id": "A-uuid", "name": "A", "reachable": True,
             "ponds": [{"id": "sales@1", "name": "sales"}], "edges": []},
            {"id": "C-uuid", "name": "C", "reachable": True,
             "ponds": [{"id": "raw@1", "name": "raw"}], "edges": []},
        ],
        "duct_edges": [{"from": {"catchment": "C-uuid", "pond": "raw@1"},
                        "to": {"catchment": "A-uuid", "pond": "raw@1"}}],
    }
    calls: list = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=a_response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(assemble_view(d, None, set(), client))
    asyncio.run(client.aclose())

    cids = {c["id"] for c in result["catchments"]}
    assert cids == {d.identity()["id"], "A-uuid", "C-uuid"}  # B + A + C (transitive)
    # The boundary edge B drew: A.sales@1 -> B.sales@1 (the local Draw node).
    assert {"from": {"catchment": "A-uuid", "pond": "sales@1"},
            "to": {"catchment": d.identity()["id"], "pond": "sales@1"}} in result["duct_edges"]
    assert len(calls) == 1  # fetched A once


def test_assemble_view_cuts_cycle_via_visited(tmp_path):
    from duckstring.catchment.routes.view import assemble_view

    d = _view_consumer(tmp_path)
    calls: list = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"catchments": [], "duct_edges": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # A is already visited (the cycle case) → no fetch, but the boundary edge is still emitted.
    result = asyncio.run(assemble_view(d, None, {"A-uuid"}, client))
    asyncio.run(client.aclose())
    assert calls == []  # cycle cut — did not recurse into A
    assert any(e["from"]["catchment"] == "A-uuid" for e in result["duct_edges"])


def test_assemble_view_unreachable_upstream_stub(tmp_path):
    from duckstring.catchment.routes.view import assemble_view

    d = _view_consumer(tmp_path)

    def handler(request):
        raise httpx.ConnectError("unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(assemble_view(d, None, set(), client))
    asyncio.run(client.aclose())
    a = next(c for c in result["catchments"] if c["id"] == "A-uuid")
    assert a["reachable"] is False


# ─── Poller end-to-end (mocked upstream transport) ─────────────────────────────


def _mock_upstream(f_iso: str, *, status="idle", tapped: list | None = None):
    """An httpx transport standing in for the upstream Catchment."""
    from duckstring.trickle_io import SIDECAR

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("orders.parquet", b"PARQUET-BYTES")
        zf.writestr(SIDECAR, '{"orders": {"mode": "append", "pk": ["id"]}}')  # a Trickle source's sidecar

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

    # Freshness mirrored, transfer landed (data + the Trickle sidecar), draw advanced.
    from duckstring.trickle_io import SIDECAR

    landed = pond_data_dir(tmp_path, "sales", 1) / "orders.parquet"
    assert landed.read_bytes() == b"PARQUET-BYTES"
    assert (pond_data_dir(tmp_path, "sales", 1) / SIDECAR).exists()  # sidecar landed → read_delta can resolve
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


def test_poller_does_not_resend_the_same_demand(tmp_path):
    # The same push target must be forwarded once, not on every cycle — re-sending it would re-add the
    # target on a mid-run upstream and cause a duplicate run.
    d = _driver(tmp_path)
    d.create_duct("up", "http://up", None)
    d.add_duct_pond("up", "sales", 1)
    d.pulse("sales@1", at=_now())  # a push target on the draw; upstream not yet fresh

    pulses: list = []

    def handler(request):
        if request.url.path == "/api/status":
            return httpx.Response(200, json={
                "ponds": [{"name": "sales", "major": 1, "end_f": None, "status": "idle"}], "edges": []})
        if request.url.path == "/api/ponds/sales/pulse":
            pulses.append(request.url.params.get("at"))
        return httpx.Response(200, json={"ok": True})

    solicited: dict = {}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(poll_once(d, tmp_path, client, solicited))
    asyncio.run(poll_once(d, tmp_path, client, solicited))  # second cycle, same unmet demand
    asyncio.run(client.aclose())
    assert len(pulses) == 1  # forwarded once across both cycles


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
