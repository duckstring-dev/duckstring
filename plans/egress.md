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
- **credential resolution** the drivers need — env-var-first (`${env:NAME}`), no bespoke vault (see Secrets).

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

**Status: built — the construct + the `file://` snapshot driver, end-to-end.** Config: migration
`008_spout.sql` `pond_spout` keyed on `pond`; `Driver.add_spout`/`list_spouts`/`remove_spout`/`resync_spout`;
`/api/ponds/{name}/spouts` CRUD + `/resync`, full-gated; CLI `duckstring spout add|ls|rm|resync {pond}`;
destination/mode validation via `egress/destination.py`. Execution: the egress-driver **seam**
(`egress/base.py` `EgressDriver`/`Capabilities`/`get_egress` scheme registry; `write_full(con, relation, …)`),
the **object-store driver** (`egress/object_store.py`, snapshot `write_full`, `supports_delta=False`) —
**`file://`** (local, atomic tmp+replace) **and `s3://`/`gs://`** (DuckDB `httpfs` + the secret manager;
credentials from the URI query `?key_id=${env:..}&secret=${env:..}&region=..`, resolved at egress time, or
the AWS credential chain for `s3://` with no key; the secret-`CREATE` error is masked so it can't echo a
credential) — and the **worker** (`catchment/egress_worker.py`, a reconciliation loop woken on
run-completion/resync — `Driver.egress_pending` keyed off the engine `end_f` vs a per-Spout **watermark**
in `pond_spout`, migration `009_spout_state.sql`, with Spout fault/retry that never fails the Pond). The **Postgres CDC driver** (`egress/postgres.py`, `postgres://`/`postgresql://`, `capabilities = {delta,
delete, transactional}`) is built: transport is the **DuckDB `postgres` extension** (`ATTACH`, then plain
DuckDB SQL against the attached tables — no SQLAlchemy/psycopg); **apply = delete-then-insert in one
transaction** (not `INSERT … ON CONFLICT` — more portable, identical Z-set net effect: delete the changed
∪ removed keys, re-insert the present rows); the **watermark lives in the destination**
(`_duckstring_egress(table_name, f)`) and is **set in the same transaction** → **exactly-once** across
Catchment crashes (a re-read after a crash is an empty/idempotent window). The worker's incremental path
reads the changelog delta over `(in-dest watermark, f]` and `apply_delta`s, falling back to `write_full`
on a full read (bootstrap / coverage-miss / changed overwrite source). The **transactional-PK gate** is
enforced at creation (`Driver._assert_transactional_pk` rejects a published non-merge/no-pk table → the
signpost error) and again at egress (the worker raises for a not-yet-checked source). The table is created
lazily from the relation's schema (DuckDB type names; the extension maps them).

Tests: `test_spout.py`, `test_egress_file.py` (file:// e2e + real Duck; s3/gs secret/target construction
unit), `test_egress_postgres.py` (the full apply/upsert/delete/reload/watermark logic against a
**DuckDB-attached** destination — the same SQL path; the PK gate; the worker delta-vs-reload routing). **Real-
backend write e2es are the CI follow-up** (MinIO/moto for s3; a containerised Postgres — both gated/skipped
locally). **Not yet built:** the incremental object-store path (per-run parts / Iceberg-in-bucket) and the
demand-aware (`--every`) schedule (reserved; the `schedule` column defaults `on-run`). A Spout name defaults
to the table (or scheme for an all-tables Spout), `-2`/`-3` on collision; `rm`/`resync` take the name.

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

**Lifecycle — a Spout is a passive standing-Wake node (the egress dual of a Pond Draw).** Conceptually a
Spout is a *Pond hanging off its source Pond with a standing Wake on it*: it delivers whenever its source's
freshness advances past what it has delivered (`sourceF > deliveredF`, and not already mid-delivery), and —
because it's a **Wake, not a Wave** — it **never solicits the source** (adds no upstream demand) and, being
**terminal**, **never blocks anything** (a downstream Pond reads the source's *published* freshness, which
advanced at publish; a sibling Spout's failure is irrelevant). It has its own freshness/run/fault log. This
is the mirror of a **Draw** (an ingress node the *poller* runs); the **egress worker** is "the Spout's
Duck." *Status: built — implemented Driver-side (`pond_spout` + `Driver.egress_pending`/`mark_egress_running`
+ the worker), deliberately **not** woven into the validated core propagation engine, since the node is
isolated by construction (non-propagating up, terminal down).* The **Control set applies, the Demand set
does not**: **Sleep**/**Kill** disarm the standing Wake (`standing_wake=0`; Kill also parks), **Wake**/**Force**
re-arm it (Force also clears the watermark to re-deliver now), **Clear** resets a fault — `Driver.spout_*`,
`POST /api/ponds/{name}/spouts/{spout}/{wake|force|sleep|kill|clear|resync}` (full-gated), `duckstring spout
wake|force|sleep|kill|clear|resync`. **An egress failure never fails the Pond Run** — it parks the Spout
(its own `is_failed`/`failures`/retry budget) and raises an [alert](#alerting-adjacent-track). Spouts are
surfaced in `/api/status` as their own nodes (`spouts[]` + a source→spout edge; `status` ∈ delivering /
queued / delivered / asleep / failed / killed; `windowed`) for the dashed-node UI. **Windows on a Spout**
throttle its Wake (`spout_window` table, migration `011`; `Driver.add/list/remove_spout_window`,
`/api/ponds/{name}/spouts/{spout}/windows`, `duckstring spout window {pond} {spout} add|list|remove`):
when it delivers, the recorded delivered-freshness is clamped to the **active window's end** (`gate_f`, not
the source freshness) so it won't re-deliver until the source passes that — at most once per window; in a
window gap it holds. The **data + CDC cursor still ride the true `sourceF`** (so a Postgres sink loses no
changelog rows that land late within a window). **Parity gap (not yet closed):** a Spout's *failures* are
state-only (`is_failed`/`failures`/`error` in `pond_spout`, shown in `status`/`ls`) — there is **no
per-delivery run-history ledger, no traceback capture, and no `/api/runs` integration** like a real Pond.
A `spout_run` log mirroring `pond_run` (+ traceback + RunHistory surfacing) is the follow-up.

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

## Access levels & key management (a prerequisite, broader than egress)

Egress makes the single-key model insufficient — you want to hand a downstream Catchment's operator the
ability to solicit demand and read, *without* giving them deploy/kill/delete. So before egress lands, split
the one key into a **total-ordered** ladder (not independent scopes — `read ⊂ demand ⊂ full`), so the check
stays one integer comparison:

- **read** — read & query data only (no ducts, no demand).
- **demand** — read/query + create demand (tap/wave/pulse/tide) + connect a downstream duct. The key you
  hand a downstream Catchment operator.
- **full** — everything: deploy, delete, the control verbs (wake/force/sleep/kill/clear/repair,
  failure-budget), window management, spouts, key rotation.

Design:

- Each key maps to a level (1/2/3); each route declares a **minimum** level; a request's level = the
  matched key's level. **Fail closed**: a route with no level annotation requires *full* — a new route
  added without classification locks down, never leaks. Prefer a per-route FastAPI dependency over
  extending the path-prefix middleware (keeps the requirement local to the route).
- **The `orchestrate` router straddles two levels** — its *demand* verbs (tap/wave/pulse/tide) are
  level `demand`; its *control* verbs (wake/force/sleep/kill/clear/repair/failure-budget) and *window*
  management are level `full`. So classify per-route there, not per-router. `data` → read; `deploy` → full;
  the duct *connection* routes → demand.
- **Backward compatibility:** a single `DUCKSTRING_API_KEY` (or `init --key`) still works and means **full**
  — the bare self-hosting floor is unchanged. The three-key ladder layers on top via init/registration.
- **Decouple the Duck's internal token from the user keys** (worth doing while in here): the Duck dial-back
  gets its **own ephemeral token** (generated at boot, in-memory, never user-facing) instead of reusing the
  api_key (today `launcher.py` → `X-Duck-Token` *is* the api_key). Then the three user keys are stored **as
  hashes** in `duck.db` (a small `catchment_key(level, hash)` table), not plaintext, and rerolling a user
  key never disrupts running Ducks.
- **Reroll** — `duckstring catchment rotate-keys [--level read|demand|full|all]` regenerates the level's
  key, replaces the `catchment_key` row, prints once. (Today rotating a key means recreating the Catchment;
  this fixes that.) The hash-table persistence the reroll needs is the same we're adding for the ladder.

**Status: built** (`catchment/auth.py`, migration `007_catchment_key.sql`; the guard is per-route deps +
`audit_routes` fail-closed at `create_app`; the Duck token is *persisted* in `catchment_meta`, not
ephemeral — a Duck must survive a Catchment restart). **UI graceful downgrade: built** —
- `/api/status` carries the caller's **`access_level`** (read off the request principal; `full` in open
  mode), threaded through the store (`accessLevel`, defaults `full` when absent) and the `atLeast()`
  ladder helper. The Sidebar gates on it: read = status/history/data only; demand = + the Triggers menu
  (tap/wave/pulse/tide + remove-trigger); full = + Control / window editing / Failures (budgets, clear,
  repair). The failure *reason* (StatusBox + Run Detail) stays visible to every level — only remediation
  is gated; window *viewing* is read-only below full, not hidden.
- **Tracebacks are full-only**, redacted server-side in `/api/runs` (`_redact_tracebacks`) — they can leak
  paths/connection strings, so read+demand get the error *message* but a null `traceback`. (Backend
  redaction, not just UI hiding — a read key hitting `/api/runs` directly is covered.)
- A small **access-level badge** sits under the catchment name in the brand panel (`DagCanvas`
  `AccessBadge`: full=green / demand=amber / read-only=grey, with a capability tooltip) so missing
  controls read as "your key can't do this", not "broken UI".

## Secrets — env-var-first, no bespoke vault

Drivers need credentials, but the OSS posture is **lean on the environment, don't reinvent a secret
store**. A bespoke encrypted `secrets.db` is theatre here: its root of trust is still an env var
(`DUCKSTRING_SECRET_KEY` guarding the file), so the encryption buys little, and — concretely — a secrets
file under the Catchment root would either leak into every `catchment archive`/`download` bundle (which
streams the whole root) or need special-case exclusion. Every platform that matters (systemd, docker, k8s,
Posit Connect, the cloud hosts) already injects secrets as env vars; that's the 12-factor path and it's
better than anything we'd ship.

So, v1 (**resolver built** — `egress/credentials.py`: `resolve()` interpolates `${env:NAME}` from the
process environment, raises `CredentialError` naming an unset var, leaves unrecognised `${...}` untouched;
`references()` lists a string's refs for pre-flight without resolving; `${secret:NAME}` is parsed-and-
reserved, raising "not yet supported". The Spout machinery will store the reference form and call
`resolve()` only at egress time):

- A Spout destination references a credential as **`${env:NAME}`** (in the URI or a credential field),
  resolved from the process environment **at egress time only** — never logged, never returned by the API.
- **No generic env-var get/set endpoint.** A *get* is an exfiltration surface (the process env holds far
  more than Duckstring's own config); a *set* mutates only the running process, doesn't survive a restart,
  and to persist it you'd rebuild the very store we're cutting. Set env the way the host platform wants.
- **Tradeoff (accepted):** a Spout to a *new* destination needs its credential present in the environment,
  so introducing one is a deploy/restart-time act, not fully runtime-dynamic. Cheap on every real target.

**Reserved escape hatch (`${secret:NAME}`, fast-follow — build only if "never SSH in" is a hard
requirement):** a *write-only* credential store closing the runtime/no-SSH gap without becoming the vault
we rejected. `duckstring secret set NAME` (prompts / `--value`) persists to a **plaintext `chmod 0600`**
file under the root (same posture as `config.toml`'s auth headers — **no encryption**, dropping the
`DUCKSTRING_SECRET_KEY` circularity), **excluded from the archive walk** (so it never leaks into a bundle).
`secret ls` shows **names only**; there is **no read-back endpoint** (write-only ⇒ not an exfil surface);
`secret rm`. Resolved as `${secret:NAME}` at egress, never logged. The `${secret:}` syntax is parsed-and-
reserved from v1 so adding the store later breaks nothing. Cloud extends this to a managed vault with
rotation + per-team scoping.

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
- `duckstring secret set|ls|rm` (the reserved write-only store — fast-follow; `ls` = names only, no get)
- `duckstring catchment rotate-keys [--level read|demand|full|all]` (regenerate + print once)
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
- Whether the write-only `${secret:}` store ships in v1 or stays reserved — decided: **reserved**
  (env-var-first; the restart-to-add-credential tradeoff is accepted). Build only if "never SSH in" hardens.

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
