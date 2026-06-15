# Cross-Catchment visibility: stable identity + recursive lineage

Status: **Phase 1 (identity) + Phase 2 (recursive view backend) built & tested**; Phase 3 (UI
overlay) pending. Builds on cross-catchment-ducts.md. Goal: every Pond's **entire** upstream lineage
is visible, recursing all the way up the ducts, with mesh cycles handled.

## Why recursion (not single-hop)

The functional coupling already propagates hop-by-hop: a failure/staleness deep in the mesh reaches a
downstream because each Draw mirrors its upstream's freshness + down-state. What's missing is
**visibility** — you can't *see* that A is waiting on something in B (or C behind A). Recursion makes
the lineage and the cause visible (the debugging value). It does not change execution.

## The enabler: stable Catchment identity (UUID)

`origin_catchment` is a local alias (A calls B "main", B calls A "compute") — names can't establish
cross-mesh identity. So:

- **Mint a UUID per Catchment** on first start, stored in the DB; never changes. Optional self
  display-name (set at `init`, may be null).
- Expose via `GET /api/catchment/identity` → `{id, name}`, and a `catchment: {id, name}` block on
  `/api/status`.
- **Show it in the UI top-left box** (this Catchment's name / short id).
- **Ducts record the upstream's UUID**: on `duct create`, fetch the upstream identity and store
  `duct.upstream_id`. This is what resolves cross-mesh identity and de-dups the A↔B round-trip.

Ponds are identified across the mesh by **`(catchment_uuid, pond_key)`**.

## Recursive lineage view (producer-orchestrated fan-out)

Only a Catchment can reach its *own* direct upstreams (it holds their creds; the poller already dials
them). So recursion fans out server-side, each hop expanding its own ducts and threading a
**visited-set of UUIDs** to cut cycles.

### Endpoint
`GET /api/view?scope=<pond keys>&visited=<csv uuids>`
- Top-level (UI → its own Catchment): no `scope` (= all local ponds), `visited` empty.
- Recursive call (Catchment → its upstream, via duct creds): `scope` = the ponds that duct draws,
  `visited` = the set so far.

### Algorithm (at each hop, for Catchment with id `self`)
1. Compute the in-scope subgraph: `scope` ponds + their **ancestors within this Catchment**
   (reuse `_ancestors`). Include each pond's live state (status/freshness/fault) and intra-Catchment
   edges. Tag every pond with `(self, pond_key)`.
2. For each duct to an upstream `u`:
   - the ponds it draws that are in-scope here = the boundary;
   - **if `u.upstream_id` ∈ `visited`**: do **not** recurse (cycle) — but still emit the duct edges
     `{from:(u_id, src_pond), to:(self, draw_pond)}` so the coupling renders against the node already
     in the merged set;
   - **else**: recurse `u/api/view?scope=<drawn ponds>&visited=visited∪{self}` using the duct creds,
     and merge the returned catchments + duct edges. On unreachable, include `u` as a stub
     `{id, reachable:false}` so the UI greys it.
3. Return `{catchments:[{id, name, reachable, ponds:[…], edges:[…intra]}], duct_edges:[{from, to}]}`,
   where each `duct_edge` is `from:(upstream_uuid, source_pond) → to:(self, draw_node)`. Merge
   catchments by UUID (union scopes if reached by multiple paths).

The local Draw node already carries its transfer state (running while copying) in the local
`/api/status`; the boundary edge can animate off it. No transfer field on the edge.

### Cycle handling (A↔B)
B→view(visited={B}) → A expands (visited={B,A}); A draws from B → B ∈ visited → A does not recurse,
emits the A←B edges. Result: containers A and B with edges both ways (a real, rendered coupling),
fetch recursion terminated by the visited-set. C behind A (B∉ path) is fetched and shown.

### Caching / cost
A naive fan-out per UI poll (~1 s) is expensive in a deep mesh. Cache **outbound** `/view` fetches
per `(duct, scope)` with a short TTL (~2 s); assemble fresh each request (assembly is cheap, network
is cached). TTL caches don't loop across a cycle (each just serves cached). The poller may warm the
cache on its existing cadence.

## UI: container nodes + cross-container edges

- Render each Catchment (by UUID) as a labelled **container/group node** (React Flow parent nodes);
  its scoped ponds are children. The local Catchment is the outer frame.
- **The Draw node stays distinct and local.** A Draw is a real operation the local Catchment performs
  — it lands real Parquet locally — so it keeps its own dashed `[DRAW]` node *inside the local
  container*, carrying its transfer state (running while copying), exactly as today. It is **not**
  collapsed into the upstream pond.
- The Draw sits **between** the upstream source pond and the local consumer: a **boundary (duct)
  edge** runs `(upstream_uuid, source_pond) → (local_uuid, draw_node)`, and the Draw's existing intra
  edges run `draw_node → local sink`. So the chain reads upstream-pond → Draw (copy) → local sink —
  faithful to reality. The boundary edge can animate off the Draw node's running state.
- No pond collapse / de-dup: the Draw and the source pond are genuinely different nodes. UUID identity
  is used only to (a) resolve a boundary edge's source endpoint to the right upstream pond node and
  (b) **merge a Catchment reached via multiple paths** (show each Catchment container once).
- Upstream containers are **read-only** in v1 (no cross-duct control; triggers/control stay local and
  keep using `/api/status`). `/api/view` is the read-only lineage overlay.
- Top-left identity box shows this Catchment's name / short UUID.

## Implementation plan

### Phase 1 — Identity primitive
1. Migration: `catchment_meta(key PRIMARY KEY, value)` KV (seed `id`=uuid4 on first start; optional
   `name` from `init`). `ALTER TABLE duct ADD COLUMN upstream_id TEXT`.
2. `GET /api/catchment/identity`; add `catchment:{id,name}` to `/api/status`.
3. `duct create` (route + CLI): fetch the upstream's identity, store `upstream_id` on the duct.
4. UI: render the identity in the top-left box (status already polled).

### Phase 2 — Recursive view (backend)
5. Producer scoping: a driver method returning the scoped subgraph (scope ponds + ancestors) with
   per-pond state + intra edges, tagged with `self` UUID; reuse `_ancestors` / `status()` internals.
6. `GET /api/view` with `scope`/`visited`; server-side fan-out over ducts using stored creds, the
   visited-set cut, unreachable stubs, and merge-by-UUID. Outbound TTL cache.
7. Boundary edge mapping: `(upstream source pond) → (local Draw node)`. The Draw node stays a distinct
   local node (its transfer state is already in `/api/status`).

### Phase 3 — UI lineage overlay
8. New `/api/view` client + store slice (separate from the local `/api/status` interactive surface).
9. Container/group nodes per Catchment; collapse Draw proxies to real ponds; cross-container duct
   edges with transfer state; `(uuid, pond_key)` de-dup; greyed unreachable containers.
10. Heed `frontend/AGENTS.md` (Next 16) and the React Flow parent-node API.

### Close-out
Tests: identity mint/stability + `upstream_id` recorded on duct create; `/api/view` scope + ancestors;
A↔B visited-set cut (no infinite recursion, both edges present); C-behind-A visible; unreachable stub.
`ruff check .`; frontend tsc/eslint.

## Notes / deferred
- Functional coupling (blocked/freshness across the mesh) already works hop-by-hop; this is
  visibility/provenance only.
- Cross-duct **control** (tapping an upstream pond from a downstream UI) is out of scope.
- Very deep meshes: depth is bounded only by the mesh; the visited-set bounds breadth re-entry.
  Acceptable for now (meshes are small); revisit caching/pagination if needed.
