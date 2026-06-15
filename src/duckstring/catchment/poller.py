"""The duct poller — the consuming Catchment's bridge to its upstreams.

One async task. Each cycle, per duct:

1. GET the upstream's ``/api/status`` and mirror each drawn Pond's freshness + reachability into its
   local Pond Draw (``Driver.observe_remote``). That cascade may start a transfer if there's
   downstream demand and the upstream is fresher.
2. Perform any pending transfers (``Driver.take_transfers``): fetch the upstream's exported Parquet
   over ``/api/draw`` and land it in the local landing zone, then report completion. Data lands
   **before** the Draw's freshness advances, so a Sink never reads stale/absent Parquet.
3. Solicit the upstream (forward a Tap) for any Draw with unmet downstream demand.

Demand flows up; data flows down. The poller is the Draw's "Duck" — Draws are never run by a
subprocess. See plans/cross-catchment-ducts.md.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime

import httpx

from .registry import pond_data_dir

_POLL_INTERVAL = 2.0  # seconds between poll cycles
_DOWN_STATES = {"failed", "killed", "blocked"}


def _parse_f(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


async def _fetch_status(client: httpx.AsyncClient, url: str, auth: dict) -> dict | None:
    try:
        resp = await client.get(f"{url}/api/status", headers=auth)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


async def _land_transfer(client: httpx.AsyncClient, url: str, auth: dict, root, name: str, major: int) -> None:
    """Fetch all of an upstream Pond line's exported Parquet and land it in the landing zone."""
    resp = await client.get(f"{url}/api/draw/{name}/{major}", headers=auth)
    resp.raise_for_status()
    data_dir = pond_data_dir(root, name, major)
    data_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for info in zf.infolist():
            if not info.filename.endswith(".parquet"):
                continue
            dest = data_dir / info.filename
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(zf.read(info))
            tmp.replace(dest)  # atomic publish


async def poll_once(driver, root, client: httpx.AsyncClient) -> None:
    targets = driver.duct_targets()
    if not targets:
        return

    # 1. Mirror upstream freshness + reachability into each Draw.
    statuses: dict[str, dict | None] = {}
    for duct in targets:
        statuses[duct["origin"]] = await _fetch_status(client, duct["remote_url"], duct["auth"])
    for duct in targets:
        status = statuses[duct["origin"]]
        by_key = {}
        if status is not None:
            for p in status.get("ponds", []):
                by_key[(p["name"], p["major"])] = p
        for m in duct["members"]:
            key = f"{m['name']}@{m['major']}"
            if status is None:  # upstream unreachable
                driver.observe_remote(key, None, down=True)
                continue
            up = by_key.get((m["name"], m["major"]))
            if up is None:  # not (yet) deployed upstream
                driver.observe_remote(key, None, down=True)
                continue
            driver.observe_remote(key, _parse_f(up.get("end_f")), down=up.get("status") in _DOWN_STATES)

    # 2. Perform pending transfers (fetch + land), then report completion.
    url_by_origin = {d["origin"]: d for d in targets}
    for t in driver.take_transfers():
        origin = _origin_for(targets, t["name"], t["major"])
        duct = url_by_origin.get(origin) if origin else None
        if duct is None:
            driver.fail_draw_transfer(t["key"], t["f"], "no duct for this Draw")
            continue
        try:
            await _land_transfer(client, duct["remote_url"], duct["auth"], root, t["name"], t["major"])
            driver.complete_draw_transfer(t["key"], t["f"])
        except Exception as exc:  # noqa: BLE001 — any failure fails the transfer (blocks downstream)
            driver.fail_draw_transfer(t["key"], t["f"], f"transfer failed: {exc}")

    # 3. Solicit upstreams for Draws with unmet downstream demand — forwarding the demand's epoch so
    #    the upstream Inlet mints the SAME freshness (push target → pulse-at-T; pull → tap-with-m).
    for d in driver.draws():
        origin = _origin_for(targets, d["name"], d["major"])
        duct = url_by_origin.get(origin) if origin else None
        if duct is None:
            continue
        try:
            if d["target"] is not None:
                await client.post(
                    f"{duct['remote_url']}/api/ponds/{d['name']}/pulse",
                    params={"major": d["major"], "at": d["target"]}, headers=duct["auth"],
                )
            if d["pull_m"] is not None:
                await client.post(
                    f"{duct['remote_url']}/api/ponds/{d['name']}/tap",
                    params={"major": d["major"], "m": d["pull_m"]}, headers=duct["auth"],
                )
        except httpx.HTTPError:
            pass  # next cycle retries


def _origin_for(targets: list[dict], name: str, major: int) -> str | None:
    for duct in targets:
        if any(m["name"] == name and m["major"] == major for m in duct["members"]):
            return duct["origin"]
    return None


async def run_poller(driver, root) -> None:
    """The poller loop. Cancelled on Catchment shutdown."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        while True:
            try:
                await poll_once(driver, root, client)
            except Exception as exc:  # keep the loop alive
                print(f"[catchment] poller error: {exc}", flush=True)
            await asyncio.sleep(_POLL_INTERVAL)
