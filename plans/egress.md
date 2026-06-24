# Egress: publishing a Pond's output to external systems

Status: **designed, unbuilt.** The OSS "last mile" — getting a Pond's output *out* of the Catchment and
into the systems a team already runs (object storage, a transactional database), with a pluggable seam
so the long tail is community- (and later cloud-) owned. Builds directly on the Trickle data layer
(`plans/trickle.md`, `src/duckstring/trickle_io.py`): **egress is a Trickle consumer whose storage is
remote**, so it reuses `read_delta` unchanged.

## The open-core line

Egress is **OSS**, because "get my data where my consumers already are" is a use-it-at-all need — gating
it is crippleware that poisons adoption. Specifically OSS:

- the **Spout** construct + the pluggable egress-driver seam (no product noun);
- two reference drivers: **object store** (S3/GCS/local, the baseline) and **Postgres** (the flagship
  *incremental* destination);
- the **secrets** store the drivers need for credentials.

Reserved for **duckstring-cloud** (it's *maintenance, credentials, and support*, not the mechanism): the
curated **managed connector catalog** (Snowflake / BigQuery / Redshift / SaaS destinations with managed,
rotated credentials), hosted delivery monitoring, and a dedicated egress worker fleet. The seam being OSS
is the flywheel: anyone can write an egress driver for their warehouse; cloud sells the catalog that's
maintained for them.

## Core idea — egress is a remote-storage Trickle read

A downstream Pond consumes a Source by `read_table` (clean current state) or `read_delta` (the change-set
over `(previous_f, f]`). **An egress target consumes a Pond the same way** — the only difference is that
it writes the result to an external system instead of a local registry. So egress is, almost literally,
`read_delta(source) → apply to remote`, and it **reuses `trickle_io` verbatim** (`read_delta`, the
`Delta` upserts/deletes, the coverage/full-read fallback).

The destination's *capabilities* and the source's *guarantees* decide the read:

| Source | Destination | Read | Apply |
|---|---|---|---|
| any (Ripple or Trickle) | object store | full snapshot, or Trickle file-sync | write the table's files (overwrite, or sync new files) |
| **merge Trickle** | **transactional (Postgres)** | **changelog window** | **`INSERT … ON CONFLICT DO UPDATE` (upserts) + `DELETE` (tombstones)** — native CDC |
| append Trickle | transactional | append window | `INSERT` the new rows |
| Ripple (overwrite) | transactional | — | **rejected at creation** (no primary key — see below) |

The merge-Trickle → Postgres row is the whole point: the changelog Duckstring already produces **is** a
CDC stream, so a modeled table syncs *incrementally* into an app's database — a few changed rows per run,
not a full reload. That makes Duckstring a legitimate reverse-ETL / CDC-sink, from machinery that already
exists.

## When the source is a Ripple (not a Trickle)

The smarts depend not on "Trickle-ness" but on a **declared primary key + change history** — a Trickle is
how you get those. So gate on the *capability requirement*, not the source type:

- **Object store** requires neither → a Spout works from **any** source. A Ripple writes an overwrite
  snapshot; a Trickle syncs only its new files. No restriction.
- **Transactional destinations** do identity-based upsert/delete → they **require a primary key**, which
  only a Trickle declares. A Ripple → Postgres Spout is **refused at creation** with a signpost error:
  *"egress to a transactional destination needs a primary key — put an `@trickle(pk=…)` before this
  Spout."*

This is "always full-overwrite for a Ripple" (option 1) framed as a PK requirement, and it's deliberate.
**Two paths considered and rejected:**

- **Full-reload a Ripple into Postgres** (truncate+load every run): correct but a trap — fine on a small
  table, a reload-the-world as it grows, and the fix is "add a Trickle" they could have added up front.
  Don't let them paint into that corner; ask for the Trickle now.
- **A hidden Trickle between the Ripple and the Spout** (auto-derive a changelog, with a PK on the Spout):
  duplicates exactly what a Trickle is, but worse — the changelog is *private to the Spout*, so it's not
  reusable by other consumers, the UI, or draws, and not versioned or contract-checked. If you want
  incremental egress from overwrite logic, the changelog belongs in a **visible** Trickle node that the
  whole graph benefits from. No shadow state; no second way to get a changelog.

The forced Trickle isn't overhead for the Spout — it's a better graph: that node gains a changelog a
second Spout, a downstream incremental Pond, and an incremental draw all reuse for free.

## The Spout construct

A **Spout** is a Pond's egress binding — "pour this table out to there." It is **operational config**
(created via CLI/API, persisted, survives redeploys), exactly like windows — *not* declared in
`pond.toml`, because destinations and credentials are environment-specific and shouldn't live in the
versioned artifact. ("Spout" fits the water metaphor and avoids the reserved **Sink** = a Pond's child.
The pluggable backend has **no product noun** — it's just the *egress driver* for a scheme, selected from
the destination URI; the directions are simply **ingress** and **egress**, both already water-themed.)

A Spout is `(pond, major, table | *, destination, mode, schedule)`:

- **destination** — a URI whose scheme selects the egress driver: `s3://bucket/prefix`, `postgres://…/schema`,
  `file:///path`. Credentials come from the URI or a referenced [secret](#secrets), never stored plain.
- **mode** — `auto` (default: incremental when the source is a Trickle and the driver supports deltas,
  else full), `full` (always snapshot), or `append`.
- **schedule** — `on-run` (default: fire after each successful Pond Run) or a staleness bound like a
  Tide (`30m` — egress at least this fresh). v1 ships `on-run` + manual resync; the Tide form is the
  natural extension once egress is demand-aware.

Lifecycle: after a Pond Run publishes, the Driver enqueues that Pond's Spouts; an async egress worker
drains them (see below). A Spout has its own fault state (`is_failed`, `failures`, retry budget) mirroring
a Pond's — **an egress failure never fails the Pond Run** (the data is published and correct locally;
egress is downstream of the boundary), it parks the Spout and raises an [alert](#alerting-adjacent-track).

## The egress-driver seam

Mirrors `dataplane.DataPlane`: a small interface, scheme-selected, that the Spout machinery threads a
`Delta` (or a full relation) through. The transform stays framework code; the user writes none. (No
product noun — an *egress driver*, like a data-plane backend.)

```python
class EgressDriver:
    def capabilities(self) -> Capabilities          # supports_delta, supports_delete, transactional
    def ensure(self, table, schema, pk)              # create/verify the destination shape (idempotent)
    def write_full(self, relation, *, table, pk, f)  # snapshot / replace
    def apply_delta(self, delta, *, table, pk, f)    # upserts + deletes (only if supports_delta)
    def watermark(self, table) -> datetime | None    # the last freshness this destination has applied
    def set_watermark(self, table, f, *, txn)        # advance it — atomically with the data when possible
```

`get_egress(uri)` resolves the driver by scheme. A driver that returns `supports_delta=False` forces the
Spout to `write_full`; one that returns `True` lets `mode=auto` use the changelog.

## Execution & exactly-once

The egress worker runs **in the Catchment process** for v1 (outbound I/O, like the duct poller — a thread
pool with per-Spout timeouts; a slow destination can't starve others). A dedicated egress *worker fleet*
is the scale path (cloud). It reuses the dial-out shape: the Catchment reaches the destination; nothing
calls back.

The watermark is the freshness a destination has fully applied. Idempotency strategy depends on the
destination:

- **Transactional (Postgres)** — store the watermark **in the destination**, in a small
  `_duckstring_egress(table, f)` table, and commit it **in the same transaction** as the upserts/deletes.
  Then egress is **exactly-once to that destination** regardless of Catchment crashes: a crash mid-apply
  rolls back; on restart the worker re-reads the same window and re-applies. (Trickle's own crash-replay
  story, one hop further out.)
- **Object store (no transactions)** — watermark in the Catchment's `duck.db`; idempotency from
  **content-addressed file writes** (a run's slice lands as `…/_f=<iso>/part.parquet`; re-writing the
  same file is a no-op). At-least-once with idempotent puts.

Bootstrap / coverage miss (the destination is empty, or behind the source's retention) → a **full read**
of the clean main and a replace/full-upsert, then resume incrementally — the same fallback `read_delta`
already implements.

## Object-store egress (the baseline)

`s3://` / `gs://` / `file://`. `capabilities = {delta: True (append-only), delete: False, transactional:
False}`. v1: mirror the data plane's published artifacts to the prefix — for a Trickle, the per-run
changelog/append files are append-only, so syncing *new* files is naturally incremental; for an overwrite
Ripple, write the snapshot. Layout option: raw Parquet, or a real **Iceberg table in the bucket** (reuses
`iceberg_plane`), which is the more useful "land it in our lake" shape. Auth via the secret-referenced
keys; uses DuckDB `httpfs` for the write. This is the floor you flagged as "probably enough for OSS
egress" — plus the existing `get`/`query`/draw read paths.

## Postgres egress (the flagship)

`postgres://user@host/db?schema=public`, password via a [secret](#secrets). `capabilities = {delta: True,
delete: True, transactional: True}`.

- **ensure** — create the destination table from the source schema if absent (DuckDB→Postgres type map),
  with the declared primary key as a PK/unique constraint (needed for `ON CONFLICT`). Plus the
  `_duckstring_egress` watermark table.
- **apply_delta** (merge Trickle) — in one transaction: `INSERT … ON CONFLICT (pk) DO UPDATE SET …` for
  `delta.upserts`, `DELETE … WHERE (pk) IN …` for `delta.deletes`, then `set_watermark(f)`. The changelog
  is already collapsed per key (latest op wins), so the apply is a clean upsert/delete set with no
  ordering hazards.
- **write_full** — stage into a temp table and swap (or truncate+load) so readers never see a partial
  load.
- Transport: DuckDB's `postgres` extension (no SQLAlchemy, consistent with the catalog decision), or
  `psycopg` if the extension's write path is too thin — to confirm at build.

This is the row that earns the feature: **continuous, incremental sync of modeled tables into an
application's transactional database**, exactly-once, from the changelog.

## Secrets

Drivers need credentials; egress is dead without a place to put them. A minimal store:

- `duckstring secret set NAME` (prompts, or `--value`) → encrypted at rest in `secrets.db` under the
  Catchment root (AES-GCM/Fernet with a key from `DUCKSTRING_SECRET_KEY`; **refuse to store without a
  key** rather than pretend), `chmod 0600` like `config.toml`. `secret ls` (names only), `secret rm`.
- Referenced from a Spout destination as `${secret:NAME}` (in the URI or a credential field), resolved
  only at egress time, never logged, never returned by the API.
- Cloud extends this to a managed vault with rotation + per-team scoping; the OSS store is the local,
  single-key version.

## Alerting (adjacent track — *not* egress, sequenced alongside)

Egress makes the need acute (a Spout fails at 3am to a flaky Postgres), but alerting is **observability,
not data movement**, and shares no code — keep it a separate, thin track:

- **Failure webhook** — on a Pond *or* Spout entering `failed`/`killed`, POST a JSON event to a
  configured URL (Slack-/PagerDuty-compatible payload). Config is operational, like a Spout.
- **`/metrics`** — a Prometheus endpoint (run latency, failure rate, freshness lag per Pond, Spout
  delivery lag) so self-hosters plug into their own Grafana/Alertmanager. Hosted dashboards = cloud.

These are the cheapest large credibility win; build them in the same milestone but keep the seam clean.

## CLI / API surface

- `duckstring spout add {pond} --to <uri> [--table T | --all] [--mode auto|full|append] [--every 30m] [--secret NAME]`
- `duckstring spout ls|rm {pond}`; `duckstring spout resync {pond} [--table T]` (force a full re-egress)
- `duckstring secret set|ls|rm`
- `/api/ponds/{name}/spouts` (CRUD) + Spout state in `/api/status` (delivery lag, `is_failed`)
- Web UI: a Spout shows on its Pond as an outbound edge with a freshness-lag badge (read-mostly, like the
  rest of the UI).

## Reuse, non-goals, risks

- **Reuse**: `trickle_io.read_delta` / `Delta` / the coverage fallback are the read half wholesale; the
  Spout is "read_delta + an egress-driver apply + a watermark." Egress from a *drawn* Trickle (cross-Catchment)
  works too, since the landing zone carries the changelog — but v1 scopes to local Outlets.
- **Non-goals**: arbitrary transformation on the way out (egress writes what the Pond published — shape it
  in a Ripple/Trickle first); managed connectors (cloud); a generic CDC *source* (this is sink-only).
- **Risks**: DuckDB→Postgres type fidelity (decimals, timestamps, nested types); the `postgres` extension
  write maturity (fallback to `psycopg`); destination schema drift vs. the Pond's contract (an additive
  Pond change must `ALTER` the destination — additive only, mirroring the version contract).

## Open questions for the build session

- Egress transport for Postgres: DuckDB `postgres` extension vs. `psycopg` for the upsert/delete apply.
- Object-store layout: raw Parquet mirror vs. a real Iceberg table in the bucket (lean Iceberg — it's the
  useful one and reuses `iceberg_plane`).
- Spout execution: in-Catchment thread pool (v1) vs. a dedicated egress worker (when writes are heavy /
  the scale path). Confirm the failure/retry budget mirrors `pond_retry`.
- Watermark home confirmed per destination (in-destination for transactional, `duck.db` for object store).
- Demand-aware egress (a Tide-shaped staleness bound) — reserve the schedule slot, build `on-run` first.
- Secret encryption: key from `DUCKSTRING_SECRET_KEY` env vs. OS keyring; behaviour when no key is set.

## Testing

- Spout CRUD + persistence + restore across restart (mirrors window/trigger tests).
- Object-store egress against a local `file://` + a MinIO/moto S3 in CI; full snapshot and incremental
  file-sync; idempotent re-put.
- Postgres egress against a containerised Postgres: `ensure` creates table+PK+watermark; merge-Trickle
  delta applies upserts+deletes; **exactly-once** under a simulated mid-apply crash (watermark rolls back
  with the data); bootstrap full-load; an additive schema change `ALTER`s the destination.
- Egress reuses `read_delta` — assert a Spout over a merge Trickle ships only the changed rows per run,
  and over an overwrite Ripple full-loads.
- Secrets: set/resolve/redacted-in-API; refuse without a key.
- Alerting: a failed Pond/Spout fires the webhook once per transition; `/metrics` exposes the gauges.
- `ruff check .` clean; e2e on a demo Pond egressing to `file://` and to Postgres.
