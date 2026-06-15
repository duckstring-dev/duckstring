"""The recursive cross-Catchment lineage view (see plans/cross-catchment-visibility.md).

`GET /api/view` returns the full upstream lineage of a Catchment's Ponds, recursing up the ducts.
Recursion is **producer-orchestrated**: each hop expands its *own* ducts (only it holds their creds),
threading a **visited-set of Catchment UUIDs** so a mesh cycle (A↔B) cuts cleanly. Each hop returns
its scoped Ponds + the boundary (duct) edges; the merge de-dups Catchments by UUID. Read-only.
"""

from __future__ import annotations

from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _merge(into: dict, sub: dict) -> None:
    """Merge a sub-result: union Catchments by id (union their Ponds + intra edges), union duct edges."""
    by_id = {c["id"]: c for c in into["catchments"]}
    for c in sub.get("catchments", []):
        existing = by_id.get(c["id"])
        if existing is None:
            into["catchments"].append(c)
            by_id[c["id"]] = c
            continue
        have = {p["id"] for p in existing["ponds"]}
        existing["ponds"].extend(p for p in c["ponds"] if p["id"] not in have)
        ek = {tuple(e) for e in existing["edges"]}
        existing["edges"].extend(e for e in c["edges"] if tuple(e) not in ek)
        existing["reachable"] = existing.get("reachable", True) and c.get("reachable", True)
    seen = {_edge_key(e) for e in into["duct_edges"]}
    for e in sub.get("duct_edges", []):
        if _edge_key(e) not in seen:
            seen.add(_edge_key(e))
            into["duct_edges"].append(e)


def _edge_key(e: dict) -> tuple:
    return (e["from"]["catchment"], e["from"]["pond"], e["to"]["catchment"], e["to"]["pond"])


async def _fetch_upstream(client, remote_url, auth, scope, visited) -> dict:
    resp = await client.get(
        f"{remote_url}/api/view",
        params={"scope": ",".join(scope), "visited": ",".join(sorted(visited))},
        headers=auth, timeout=httpx.Timeout(15.0, connect=5.0),
    )
    resp.raise_for_status()
    return resp.json()


async def assemble_view(driver, scope: Optional[list[str]], visited: set[str], client) -> dict:
    """This hop's fragment + recursion into its non-visited upstreams (using each duct's creds)."""
    frag = driver.view_fragment(scope)
    self_id = frag["catchment"]["id"]
    visited = visited | ({self_id} if self_id else set())
    result = {
        "catchments": [{
            "id": self_id, "name": frag["catchment"]["name"], "reachable": True,
            "ponds": frag["ponds"], "edges": frag["edges"],
        }],
        "duct_edges": [],
    }
    for duct in frag["ducts"]:
        uid = duct["upstream_id"]
        for pk in duct["drawn"]:  # boundary edge: upstream source Pond → local Draw node (same key)
            result["duct_edges"].append({
                "from": {"catchment": uid, "pond": pk}, "to": {"catchment": self_id, "pond": pk},
            })
        if uid and uid not in visited:  # else: cycle — edge already emitted, don't recurse
            try:
                sub = await _fetch_upstream(client, duct["remote_url"], duct["auth"], duct["drawn"], visited)
                _merge(result, sub)
            except httpx.HTTPError:
                if uid not in {c["id"] for c in result["catchments"]}:
                    result["catchments"].append(
                        {"id": uid, "name": None, "reachable": False, "ponds": [], "edges": []}
                    )
    return result


@router.get("/view")
async def view(request: Request, scope: Optional[str] = None, visited: Optional[str] = None):
    driver = getattr(request.app.state, "driver", None)
    if driver is None:
        raise HTTPException(status_code=503, detail="driver not ready")
    scope_list = [s for s in scope.split(",") if s] if scope else None
    visited_set = {v for v in visited.split(",") if v} if visited else set()
    async with httpx.AsyncClient() as client:
        return await assemble_view(driver, scope_list, visited_set, client)
