# Flock: a self-hosted DuckDB server over Quack

Status: **planned (2026-10-22)**, branch `quack-flock`. Picks up item 6 of `plans/duckdb-2-ready.md` and the
Quack verdict in `plans/duckdb-2.md` (Part 3), now that DuckDB 2.0 ships Quack as stable.

## Why

The Flock sends a Trickle terminal's wholesale recompute to a bigger engine (bootstrap, rebuild, a coverage
miss, an over-`p` source, every `.sql()` terminal on every run). The DuckDB-based engine is the clean one: the
job's SQL runs unchanged, so every job is eligible and results match by construction, with no translator,
measured construct lists, acceptances or sampled verification. Today that engine is MotherDuck, which means a
third-party account and data leaving the user's cloud account.

A DuckDB server in the user's own account, reached over Quack, gives the same properties with no third party,
and Duckstring ships both ends, so it controls the DuckDB version. Most of the engine already exists:
`engines/remote_duckdb.py` is written against "a connection whose queries run remotely", and MotherDuck is a
62-line subclass.

Alternatives considered (see the conversation summarised in `plans/duckdb-2.md`): a bigger Duck (pays for the
big machine every run), a one-off big task per recompute (no server, scales to zero, but a minute or more of
start-up per recompute and a new job runner), a self-hosted SQL engine (StarRocks already works, with all of
its translation machinery). The server wins on correctness and warm latency; phase 2's on-demand start takes
most of the scale-to-zero advantage.

## Spike (2026-10-22, DuckDB 2.0.0, server and client on one laptop)

- **Serving**: `CALL quack_serve('quack:localhost:PORT', token := '...')` from Python returns at once and serves
  from a background thread. A wrong token fails `ATTACH` with "Authentication failed".
- **Upload**: a local Arrow reader registered on the client, then `CREATE TABLE fl.main.t AS SELECT * FROM view`
  over the attached catalog: 5M rows (4 columns) in 106 to 152 ms. The engine's `_upload` works as written.
  (2.0 rule: the local relation must be on another connection, since an open Arrow reader owns its
  connection. The engine already uses two.)
- **Running the job**: `SELECT * FROM quack_query_by_name('fl', $q$<job sql>$q$)` runs the whole query on the
  server, including table functions such as `read_parquet(...)` (verified with a file only the server's
  working directory had), joined to an uploaded table. The result streams back with `to_arrow_reader`: 5M rows
  in 96 ms. Types come back as Arrow carries them (`HUGEINT` as `DECIMAL(38,0)`); `conform` already casts to
  the oracle's schema.
- **What doesn't work**: `CONNECT` routes table functions to the server, but while connected the client can't
  register views or produce Arrow (`materialize() is not available while CONNECT-ed`). So the engine never
  uses `CONNECT`.
- **Plain attach pushes down**: a query over only attached tables runs on the server (a 5M-row aggregate in
  5 ms). A query mixing local and remote objects would be planned locally, which is why the job goes through
  `quack_query_by_name`.
- **Versions must match**: a DuckDB 1.5.6 client against the 2.0 server fails (`Serialization Error: Failed to
  deserialize`). Whether patch versions within 2.0 interoperate is unknown.
- **Concurrency**: four clients querying at once all succeeded.

Not measured: throughput over a real network, TLS (Quack defaults to HTTPS with a self-signed certificate for
non-local addresses), behaviour under memory pressure with several large jobs.

## Design

### The engine (`flock/engines/duckdb_server.py`)

`DuckDBServerEngine(RemoteDuckDBEngine)`, engine name `duckdb-server` (with profiles, `duckdb-server/<name>`, as
MotherDuck has them, for a small and a large server).

- `connect()`: an in-memory DuckDB, `LOAD quack`, `ATTACH 'quack:<address>' AS duckstring_flock (TOKEN ...)`
  (plus `SSL_FINGERPRINT` or `DISABLE_SSL` per config). The attach is the only target-specific step.
- `scratch()`: `duckstring_flock.main`.
- The base class's `_fetch` changes from `remote.execute(sql)` to a hook, `run_remote(remote, sql)`, so this
  engine wraps the job in `quack_query_by_name('duckstring_flock', ...)` while MotherDuck keeps its own path.
  Dollar quoting with a per-job tag (`$dsq_{tag}$`) so the job's SQL can't close the quote.
- **Version check** on first use per process: the server's `library_version` (via `quack_query_by_name`)
  must equal the Duck's. A mismatch disables the engine with a clear `flock_error` ("Flock server runs DuckDB
  X, this Duck runs Y"), so nothing is dispatched to a server that would fail or, worse, answer differently.
- Reads in place: `Pond.flock_source_select` already yields `read_parquet('s3://...')` for Sources current on
  the object-store data root; on this engine they run server-side, so the server needs S3 access (below).
- `eligible`, `estimate_rows`, the row envelope, `conform`, fallback and the dispatch counters are unchanged.

### Configuration

| Setting | Where | Meaning |
|---|---|---|
| `DUCKSTRING_FLOCK_SERVER` (`_<PROFILE>`) | Catchment env, sent on the job | `host:port` of the server |
| `FLOCK_SERVER_TOKEN` (`_<PROFILE>`) | secret store, sent on the job (`SECRETS`) | the Quack token |
| `DUCKSTRING_FLOCK_SERVER_FINGERPRINT` | Catchment env | pins the server's self-signed certificate |
| `DUCKSTRING_FLOCK_SERVER_TLS` | Catchment env | `off` for plain HTTP inside a private network |

Every `DUCKSTRING_FLOCK_*` setting already travels to the Duck on its `begin_run` job (`flock.job_settings`),
and an engine's `SECRETS` are sent only when the Flock is on, so Fargate, EC2 and pool Ducks need nothing new.
`enabled()` = an address and a token. `ENGINE_ORDER` becomes `motherduck, duckdb-server, starrocks, athena`
(the order `plans/duckdb-2-ready.md` set; it only matters when several are configured and none is named).

### Running the server (`duckstring duck flock-server`)

A foreground command (under `duck`, the compute group, beside `duck pool`), for a large machine in the Ducks'
network:

- Options: `--port` (default 9494), `--host` (default `0.0.0.0`, which needs `allow_other_hostname`),
  `--memory-limit`, `--threads`, `--database` (a file, so uploads and large intermediates can spill; default
  under a temp directory, deleted at exit), `--token-env` (read the token from an environment variable; never
  an argument).
- At start: `LOAD quack`, `LOAD httpfs`, an S3 secret from the machine's credential chain (an instance role,
  never a key on disk), honouring `DUCKSTRING_S3_ENDPOINT` the way the data plane does, then `quack_serve`.
- Logs one line per job (from Quack's logging: connection, query time, rows), so an operator can see use.
- Scratch tables are dropped by the engine after each job; at start the server drops any `dsflock_*` left by a
  Duck that died mid-job.

### Security

- One token, the secret store's `FLOCK_SERVER_TOKEN`, also given to the server at start (`--token-env`).
  Rotating it means restarting the server and setting the secret; Ducks pick it up on their next run.
- The server is reachable only from the Ducks: on AWS a security group allowing the port from `sg-duck` only
  (documented next to the existing three roles and two groups in the AWS guide).
- TLS: Quack serves HTTPS with a self-signed certificate for a non-local address. The engine pins it with
  `SSL_FINGERPRINT` when configured; `DUCKSTRING_FLOCK_SERVER_TLS=off` allows plain HTTP for a private network.
  A spike item: confirm the fingerprint flow end to end.
- The server runs arbitrary SQL from anyone holding the token, with the machine's S3 access. That's the same
  trust as a Duck, and the docs say so.

## Phase 2: start on demand

The server is a big machine, so it should run only while there's work.

- The Catchment starts it through the EC2 launcher (a `flock_server` pool entry: instance type, AMI, network),
  with userdata that installs the Catchment's Duckstring version (same DuckDB by construction) and runs
  `duck flock-server --idle-exit 15m`. The server powers itself off after an idle period, as an EC2 Duck does.
- When a Pond with this engine starts a Run, the Catchment ensures the server is starting and sends its
  address on the job once it answers. Until then the engine reports not-ready and the job runs locally (the
  existing fallback), so a cold start never blocks a run.
- Separate PR; phase 1 is usable on its own with a server the user runs.

## Tests

- `tests/test_flock_duckdb_server.py`: a real Quack server in-process on localhost (skipped where the `quack`
  extension isn't available), running the same cases as `tests/test_flock_remote.py`: in-place reads (a
  `reads_local_paths` stand-in), uploads, `.sql()` extra tables, aggregates, `conform`, the fallback on a dead
  server, a wrong token, a version mismatch (simulated), concurrent dispatches.
- A runtime e2e: a Trickle demo Pond with `flock always` against a local server, checking the dispatch counter
  and identical output to a local run.
- CI: the main job (DuckDB 2.0) runs both; the floor job (1.5) skips them.

## Docs

- `guides/offloading_large_recomputes.md`: the engine, when to choose it over MotherDuck, running the server.
- `guides/cloud_compute_on_aws.md`: an EC2 server in the Ducks' VPC, its security group and instance role.
- `reference/cli/duck.md` (`duck flock-server`, matching the command's help), `reference/environment.md` (the
  settings above), `reference/pond_toml.md` (`[flock] engine = "duckdb-server"`).

## Open questions for the author

- The engine's name: `duckdb-server` (describes what it is) or `quack` (the protocol).
- The command's home: `duck flock-server` (proposed) or a new top-level `flock` group.
- Engine order when several are configured: after MotherDuck (as planned before) or first, since it's in the
  user's own account.

## Order

1. Spike the remaining items: TLS with a pinned fingerprint between two machines, patch-version
   compatibility, a large job under memory pressure.
2. The engine and its tests.
3. `duck flock-server` and the AWS docs; a manual run on the AWS smoke-test account (server on EC2, Fargate
   Ducks dispatching to it).
4. Phase 2 in a later PR.
