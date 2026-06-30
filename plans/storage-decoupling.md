# Storage decoupling — object-store data, ephemeral hot state, scale-to-zero

**Status:** Phase 1 + Phase 2 (Tier-1) **implemented**. Motivated by hosting a Catchment on a cloud node
whose local disk is ephemeral (Databricks Apps, a scale-to-zero container) and whose durable storage is an
object store (S3/GCS/ABFS) or a Databricks Volume.

**What shipped:**
- `src/duckstring/storage.py` — the `Storage` seam (`LocalStorage` + `ObjectStorage`/fsspec), `${env:}`
  credentials, DuckDB `httpfs` setup. `pond_data_dir(state_root, name, major, data_root)` returns a
  `Storage`; the **parquet** data plane + the trickle fs helpers + draw/poller/egress/data routes all run
  through it.
- **Iceberg over object storage** — `FileCatalog`'s `catalog.json` pointer is routed through the `Storage`
  seam (a single-object atomic PUT, no `os.replace`); `_catalog` uses `warehouse_location()` (the object
  URI) and threads the resolved `${env:}` creds into pyiceberg FileIO `properties` (`s3.*`/`gcs.*`/`adls.*`);
  the orphan-file GC sweeps via the seam (`data_dir.child("pond", table, sub).names()`). So **both** data
  planes work on an object-store data root; Iceberg stays the default everywhere.
- **Data-root writer lease** (`src/duckstring/catchment/data_lease.py`) — a `_duckstring_owner.json` object
  at an external data root, keyed to the Catchment `id`, acquired at boot / renewed / released on shutdown.
  Refuses to start a *different* live Catchment on the same lake (the case single-writer-per-line can't
  cover) so two writers can't race the Iceberg pointer; a same-id restart reclaims instantly, an expired
  lease is taken over (TTL 120 s), `DUCKSTRING_FORCE_TAKEOVER=1` steals it. A portable plain-PUT lease with
  read-back race detection — a *detector/narrower*, not a CAS mutex (engaged only for an external data root).
- The **state/data root split**: env vars `DUCKSTRING_STATE_ROOT` (alias `DUCKSTRING_ROOT`),
  `DUCKSTRING_DATA_ROOT`, `DUCKSTRING_STATE_BACKUP_URI`, `DUCKSTRING_CHECKPOINT_INTERVAL`; `catchment init`
  `--data-root`/`--state-backup`/`--checkpoint-every` (+ validation, persisted in the registration so
  `start` reuses them); `catchment restore`; `download` scoped to the state root.
- **Tier-1 state sync** (`src/duckstring/catchment/state_sync.py`): a checkpoint worker pushing a `duck.db`
  snapshot on an interval, a graceful-shutdown flush that also bundles the (now-quiescent) registries +
  ledgers (`state.tar`), and boot-time auto-restore in `create_app`.
- Tests: `tests/test_storage.py`, `tests/test_state_sync.py`; the existing suite is green (the data plane
  runs through `LocalStorage`, proving the port is behaviour-neutral on the local path).

**Deferred / CI follow-up** (same posture as the egress real-backend tests): real `s3://`/MinIO/moto e2e
of the DuckDB-over-`httpfs` + pyiceberg-FileIO read/write path (the fsspec metadata ops are unit-tested
against `memory://`; the full parquet **and** Iceberg pipelines against a separate **local** data root,
i.e. the Volume-FUSE case); the catalog-pointer **conditional PUT** (the writer lease now guards the
two-Catchment case at boot; a per-commit CAS would make it an airtight mutex rather than a detector —
deferred, needs backend-specific conditional-write); **Tier-2 as a live cache**
(publishing the accumulator companions to the data plane / quiescent registry sync mid-run); a **local
read-cache dir** for cold object reads.

## Problem

The runtime has been built assuming the Catchment root is one local POSIX filesystem: `duck.db`
(SQLite), the per-Pond `pond.db` ledgers, the `registry.duckdb` working registries, **and** the data
plane's published Parquet/Iceberg all live under one `DUCKSTRING_ROOT`. On a cloud node that assumption
breaks two ways:

1. **The data files almost never want to be on local disk.** They're the durable interchange layer and
   they're large; on S3-backed compute they belong in the bucket.
2. **The node's local disk is ephemeral.** Scale-to-zero (or a redeploy) loses everything not synced
   out — including the engine state the whole pipeline's correctness depends on.

But you **cannot** simply relocate the root to S3 or a Volume FUSE mount: SQLite and DuckDB require POSIX
semantics (byte-range writes, `fsync`, mmap, advisory locking) that object stores don't provide and that
FUSE-over-object-store (DBFS, Databricks Volumes) emulates with broken rename atomicity and no real
locking. SQLite-on-DBFS corruption is a well-known footgun.

## The two storage classes

The codebase already cuts along the right boundary — the registry / data-plane split
([`dataplane.py`](../src/duckstring/dataplane.py): "Ripples always compute on the registry; the data
plane is the export/interchange layer only"):

| Class | Files | Needs | Goes |
|---|---|---|---|
| **Hot state** (POSIX) | `duck.db`, `pond.db` ledgers, `registry.duckdb`, `config.toml`, registry-only `_duckstring_agg_*`/`_duckstring_acc_*` companions | byte-range writes, `fsync`, locking | local/ephemeral disk |
| **Data blobs** (write-once / atomic-overwrite) | everything under [`pond_data_dir`](../src/duckstring/catchment/registry.py#L22): parquet parts, Iceberg metadata + data files, `__base/` chunks, `__band/` bands, `_trickle.json` sidecars, the catalog | object-level atomic PUT only | object store / Volume |

**Hard rule: hot state stays on local disk; we get durability by syncing snapshots out, never by
relocating the live files** (the Litestream/LiteFS pattern).

The single load-bearing invariant throughout is the existing **"run exactly ONE app process"**
(`asgi.py`). Single-writer-per-line is what makes object-store commits safe without distributed locks and
makes scale-to-zero (a *serial* stop→start) the only concurrency story we support. This plan does **not**
introduce horizontal scale-out.

---

## Part 1 — URI-addressable data plane

The bulk of the value, and well-contained: `data_dir` becomes a storage location (URI), not a `Path`,
and the data plane stops assuming a local filesystem. Only [`dataplane.py`](../src/duckstring/dataplane.py),
[`iceberg_plane.py`](../src/duckstring/iceberg_plane.py), [`iceberg_catalog.py`](../src/duckstring/iceberg_catalog.py),
and [`registry.py`](../src/duckstring/catchment/registry.py)'s path helpers touch it.

### 1a. A `Storage` seam

A thin interface — `put_atomic / get / list / delete / exists / open` — with two impls:

- `LocalStorage` — today's behaviour (a `Path` root; `tmp.replace(dest)` rename).
- `ObjectStorage` — `fsspec`-backed (covers `s3://`, `gs://`, `abfss://`, and Databricks Files via the
  `databricks` filesystem). Atomic-overwrite = a single-object PUT (object stores make this atomic; the
  rename dance is unnecessary). `list` = list-prefix; `delete` = delete-object.

`pond_data_dir(root, name, major)` returns a storage URI/handle rather than a `Path`. **SQLite and DuckDB
paths never route through `Storage`** — they keep using `registry.py`'s local-`Path` helpers
(`pond_registry_path`, `pond_major_dir`) against the **state** root.

### 1b. The data plane is already object-store shaped

Most of [`dataplane.py`](../src/duckstring/dataplane.py) ports mechanically:

- **Append-only parts** (`_export_parts`, [:315](../src/duckstring/dataplane.py#L315)) are immutable,
  name-addressed files → a PUT, no rename. `tmp.replace(dest)` → `put_atomic`.
- **Overwrite tables** ([:267](../src/duckstring/dataplane.py#L267)) → a single-object atomic PUT (no
  tmp+rename needed — object PUT is atomic at the object level).
- **Base chunks** (`_publish_base_chunks`, [:455](../src/duckstring/dataplane.py#L455)) are *already*
  designed lock-free and overlap-safe via per-checkpoint **tokens** (write under a new token prefix, then
  list-and-delete the other tokens). That's an object-store reconcile, not a rename — it ports almost
  verbatim: `src.replace(dest)` → PUT, `glob` → list-prefix, `unlink` → delete-object,
  `shutil.rmtree(staging)` → delete-prefix. The sidecar's `f_base` still advances only *after* the new
  chunks land, so a reader momentarily seeing both token generations reconstructs idempotently.
- **Warm bands** (`_export_bands`, [:428](../src/duckstring/dataplane.py#L428)) — same, immutable per
  fold.

DuckDB already **writes** object storage natively: `COPY … TO 's3://…' (FORMAT PARQUET)` over `httpfs`
with the secret manager — the [`s3://` egress driver](../src/duckstring/egress/object_store.py) is the
proven template (down to masking the credential-`CREATE` error so it can't echo a secret). Reads are
`read_parquet('s3://…', union_by_name=true)` / `iceberg_scan` over the same. `FILE_SIZE_BYTES` chunking
on `COPY … TO 's3://prefix'` writes a directory of objects, exactly as locally.

### 1c. Iceberg over object storage

pyiceberg already does S3/GCS/ABFS `FileIO`. The only wrinkle is `FileCatalog`'s `catalog.json` pointer
([iceberg_catalog.py](../src/duckstring/iceberg_catalog.py)): the local `os.replace` becomes a
single-object **conditional PUT** (S3 `If-Match`/`If-None-Match`, GCS generation-match). Single-writer-
per-line means we don't strictly need the CAS — it's hygiene against a misconfigured second writer.
`catchment archive`'s root walk already includes `catalog.json`; with an object-store data root it isn't
in the state root at all (it's already durable), so archive only ever covers the **state** root (see
Part 2).

### 1d. Credentials

Reuse the egress **`${env:NAME}`** convention ([egress/credentials.py](../src/duckstring/egress/credentials.py)):
object-store credentials are env-var references in the data-root URI query
(`s3://bucket/prefix?region=…&key_id=${env:AWS_KEY}&secret=${env:AWS_SECRET}`), resolved **only at
runtime**, never stored in `config.toml`. With no explicit key, fall back to the ambient credential chain
(instance profile / Databricks credential passthrough / `abfss` managed identity). Ducks are subprocesses,
so the resolved credentials must be threaded into their environment (the launcher already passes env).

**Outcome of Part 1:** data lives in the bucket / Volume, hot state on ephemeral disk. Data is durable;
the node still can't scale to zero without losing the engine — that's Part 2.

---

## Part 2 — Tiered state sync (scale-to-zero)

Don't sync the state root as one blob. Tier it by recoverability:

- **Tier 0 — data plane.** Already durable in object storage after Part 1. Nothing to sync. Note the
  consequence: the durable Trickle incremental state (changelog / base / bands) lives here, **not** in the
  registry — so losing a registry on scale-to-zero is mostly *recompute cost*, not data loss.

- **Tier 1 — `duck.db`. Mandatory, small, frequent.** Everything `Driver.reload` needs to reconstruct
  the engine + run history. We already snapshot it consistently via the SQLite **backup API**
  ([`routes/catchment.py`](../src/duckstring/catchment/routes/catchment.py#L99)). Generalize that into a
  *push* to `DUCKSTRING_STATE_BACKUP_URI` on an interval and on graceful shutdown. **Strongly consider
  Litestream-style continuous WAL shipping** here instead of periodic snapshots — near-zero RPO without
  quiescing, with point-in-time restore. Either works at this DB's size; Litestream removes the "did we
  lose the last N minutes" question.

- **Tier 2 — registries + `pond.db` ledgers + the registry-only `_duckstring_agg_*`/`_duckstring_acc_*`
  accumulator companions. Best-effort cache, rebuildable.** A missing registry is a `refresh`
  (wipe-and-rebuild) + a comprehensive aggregate rebuild. DuckDB has no online-backup equivalent, so sync
  these by **file copy when a line is quiescent** (no Duck writing it), purely to make restart *warm*. On
  a miss → cold rebuild: slower, still correct. The one durable-ish registry-only datum is the accumulator
  companions; **publishing them to a private data-plane prefix** demotes Tier 2 to a pure cache and stops
  scale-to-zero forcing agg rebuilds (optional follow-up).

### Mechanism

- A **checkpoint worker**: an async loop in the Catchment process, modelled on the egress worker / poller
  ([`egress_worker.py`](../src/duckstring/catchment/egress_worker.py)), woken by `Driver._signal_*` and
  self-healing on a tick.
- A **lifespan shutdown hook** ([`app.py`](../src/duckstring/catchment/app.py) lifespan) flushing Tier 1
  on graceful stop.
- **Restore-on-boot** in [`asgi.py`](../src/duckstring/catchment/asgi.py) / `create_app`: if the state
  root is empty and a backup URI is set, pull Tier 1 (and Tier 2 if present) **before** `migrate()` /
  `reload`.

---

## Specifying disk: config & CLI

**Principle (matches windows/spouts):** disk location and credentials are **environment-specific
operational config — never in `pond.toml`**. A deployed Pond carries code + its source pins; *where* its
output lands is the Catchment's concern. So **deployment does not specify disk**; the Catchment does, once,
at establish time. `duckstring pond deploy` is unchanged.

### Environment variables (the platform-hosting surface)

`asgi.py` stays env-configured. Today's `DUCKSTRING_ROOT` is **renamed-with-alias** to make the split
explicit:

| Var | Meaning | Default |
|---|---|---|
| `DUCKSTRING_STATE_ROOT` (alias: `DUCKSTRING_ROOT`) | local POSIX root for hot state (`duck.db`, ledgers, registries) | `./.duckstring` |
| `DUCKSTRING_DATA_ROOT` | URI for the data plane (`s3://…`, `gs://…`, `abfss://…`, `/Volumes/…`, or a local path) | the state root's `ponds/` (today's behaviour) |
| `DUCKSTRING_STATE_BACKUP_URI` | where Tier-1/2 checkpoints sync | unset → no sync (single-node, non-ephemeral) |
| `DUCKSTRING_CHECKPOINT_INTERVAL` | Tier-1 sync cadence (e.g. `30s`); ignored under Litestream | `60s` |
| `DUCKSTRING_DATA_PLANE` | existing — `iceberg` / `parquet` | `iceberg` |

When all three location vars point under one local root, behaviour is byte-for-byte today's.

### `catchment init` — establishing a Catchment

New options, all optional (omitted → local single-disk, as now):

```
duckstring catchment init --name prod \
  --host 0.0.0.0 --port 7474 \
  --root /local_disk0/duckstring \                         # state root (local, ephemeral)
  --data-root 's3://acme-lake/duckstring?region=eu-west-1' \  # data plane → bucket
  --state-backup 's3://acme-lake/duckstring-state' \       # Tier-1/2 checkpoints
  --checkpoint-every 30s
```

- `--root` keeps its meaning (the **state** root); help text clarified to "local hot-state directory".
- `--data-root` / `--state-backup` accept a URI with `${env:NAME}` credential refs (validated for scheme
  + credential-ref *syntax* at init, like a Spout destination — resolved only at runtime). Stored in the
  registration (`config.toml`) **as written** (refs, not values).
- `--checkpoint-every` parses the same duration grammar as a Tide/window bound.
- On a cloud platform you set the **env vars** instead and ship the two-file bundle; `init` is the
  local/CLI front door for the same settings.

### `catchment start` / `download` / restore

- `start` reads `data_root` / `state_backup` / `checkpoint_every` from the registration (already loads
  `root`, `key`, `headers`) and threads them into `create_app`.
- `catchment download` ([:368](../src/duckstring/cli/catchment.py#L368)) already pulls the whole root; it
  becomes **state-root only** (the data plane is durable on its own) and the message says so.
- New `catchment restore [--from URI] [--path DIR]` — the explicit inverse of the boot-time auto-restore,
  for seeding a fresh node by hand.

### Validation

`init` warns loudly if `--root` (state) is set to an object-store/Volume URI — that's the SQLite-on-FUSE
footgun. State root must be a local path; data root and backup URI must be object-store-class (or local,
for dev).

---

## Databricks Volumes specifically

Two ways to use a Volume; prefer the second for data:

1. **FUSE path** (`/Volumes/cat/schema/vol/…`) treated as local disk — acceptable *only* for the data
   plane (whole-file writes), **never** for the state root, and even then distrust its rename atomicity
   and eat the latency. This is the zero-config fallback (`--data-root /Volumes/…`, `LocalStorage`).
2. **Address the backing object store directly** (the Volume's external location is `s3://`/`abfss://`),
   or use the Databricks Files API through `fsspec` — reuses Part 1 wholesale, avoids FUSE semantics.

Put `--state-backup` on a Volume (or its bucket) too. Databricks Apps gives ephemeral local disk
(`/local_disk0`) for the state root — exactly what Tier 1/2 want.

---

## Phasing

1. **Storage seam + URI data root** — `Storage` interface, object-store-clean Parquet + Iceberg planes,
   `${env:}` credentials, `--data-root` / `DUCKSTRING_DATA_ROOT`. Ships durable-data value on its own.
2. **Tier-1 checkpoint/restore** — generalize the archive into push/pull against a backup URI (or wire
   Litestream), the checkpoint worker, shutdown hook, boot-time restore, `--state-backup`. Unlocks
   scale-to-zero.
3. **Tier-2 warm cache** — quiescent registry/ledger sync + publish the accumulator companions. Makes
   restart fast, not just correct.

## Caveats / open questions

- **Read latency** on cold object storage is real; the parts-pruning + predicate-pushdown model tolerates
  it, but a hot Pond reading a large foreign Source every run will want a **local read-cache dir** —
  deferred past Phase 1.
- **DuckDB registry snapshot consistency** — no online backup, so Tier 2 is "copy when quiescent or
  rebuild on restore." A real limitation, not a bug to engineer away.
- **Cross-Catchment draw** (`routes/draw.py` / poller) stays HTTP and unchanged — it reads through
  whatever the producer's data plane resolves, so it's free.
- **`catchment archive` scope** — once the data root is external, archive covers only the state root;
  confirm `usage`/`archive_bytes` accounting follows.
- **Litestream vs. roll-our-own** for Tier 1 — Litestream is an external process (a sidecar), which a
  single-binary `duckstring catchment start` doesn't have a natural home for; the interval-snapshot path
  is self-contained. Decide per deployment target (platform bundle can run a sidecar; the CLI launcher
  can't easily).
- **Docs**: `running-a-catchment` gains a "Cloud / ephemeral disk" section; the `pond.toml`-has-no-disk
  principle goes next to the windows/spouts "operational config" note.
```
