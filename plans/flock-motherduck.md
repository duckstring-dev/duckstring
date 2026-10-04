# The Flock on MotherDuck

Status: **settings-in-the-job part built (2026-10-03)**, see "As built" at the end; the engine waits for
the spike, which needs a MotherDuck account. Designed 2026-10-02 and agreed with the author: MotherDuck becomes the first-class
Flock engine. Athena stays, for teams that need compute inside their own AWS account. `plans/duckdb-2-ready.md` (item 6)
generalises this engine to a remote-DuckDB engine with a second, self-hosted target (a DuckDB 2.0 server
in the user's account); build the two together.

## Why MotherDuck

The Flock sends a `pond.trickle(...)` terminal's comprehensive recompute to a serverless engine, and the
Duck keeps the merge, diff and publish. DuckDB is the authority on what gets published, so with Athena
(Trino) most of the Flock's machinery exists to keep two engines from disagreeing:

- an expression allow-list (`flock/equivalence.py` plus Athena's lists): no division (DuckDB `7/2` is 3.5,
  Trino's is 3), no `CAST` (rounding differs at .5), no string functions;
- a narrow v0 shape: left-deep equi-joins only, no `.mutate`, `.aggregate`, `.accumulate` or `.sql()`;
- staging: each source is copied to a scratch prefix and registered as a Glue table, then the composition
  is recompiled into Trino SQL (`athena._compile_select`).

MotherDuck runs DuckDB. The builder's own SQL runs unchanged, so every expression is equivalent by
construction, `.mutate` and `.sql()` become dispatchable, and with an object-store data root MotherDuck
reads the sources straight from the bucket with no staging. `conform` (cast to DuckDB's bound schema,
reject a differing column set) stays as the guard against DuckDB version differences.

What a cloud Duck still does better: it moves the whole Pond, Python included, next to the data, and it
keeps the data inside the user's AWS account. The Flock only offloads the heavy read side of a builder
recompute; the Duck still holds the result to merge and publish it.

## MotherDuck facts the design depends on

Checked 2026-10-02 against MotherDuck's documentation:

- A **Duckling** is the per-user or per-service-account DuckDB instance. Sizes: Pulse (billed per query),
  Standard, Jumbo, Mega, Giga (billed per second of wall-clock time, about $2.40, $4.80, $12 and $24 an
  hour). Memory and CPU per size aren't published.
- **Size is set per user or service account.** Changing it takes 2 minutes (up to Jumbo), 5 (Mega) or 10
  (Giga). A Duckling of a fixed size starts in about 100 ms (up to Jumbo) or a few seconds (Mega, Giga).
- **Cooldown**: a Duckling stays warm, and billed, after its last query: default 1 minute (Standard,
  Jumbo), 5 (Mega), 10 (Giga); configurable from 1 minute to 24 hours. So one short dispatch on a Giga
  account with the default costs about $4; with a 1-minute cooldown, about $0.40.
- Regions: US East, US West, Frankfurt, Dublin, Tokyo, Sydney. It can read the customer's own S3, GCS or
  Azure storage.

## Design

### Engine: `flock/engines/motherduck.py`

A built-in engine named `motherduck`, implementing the existing `FlockEngine` protocol
(`enabled`/`eligible`/`estimate_rows`/`dispatch`).

- **Credentials.** A MotherDuck token: the secret `MOTHERDUCK_TOKEN` (the write-only store), else the env
  var `motherduck_token` (MotherDuck's own name). Never on a command line.
- **Accounts per size ("kicking up").** Resizing an account is too slow to do per dispatch, so size is
  chosen by choosing an account. The engine name may carry a profile: `motherduck/giga` uses the secret
  `MOTHERDUCK_TOKEN_GIGA`. The per-Pond engine setting already exists (`pond.toml [flock] engine`,
  `duckstring duck set --engine`), so a heavy Pond points at the large account and the rest use the
  default. The docs recommend a 1-minute cooldown on these accounts.
- **`enabled()`**: a token is available, and the data root is an object store MotherDuck can reach
  (`s3://`, `gs://`). On a local data root it's disabled: MotherDuck can't see the files, and uploading
  them through the client would defeat the point.
- **`eligible(builder)`**: everything the builder compiles to SQL: every join `how` and shape (bushy
  included), `.filter`, `.mutate`, `.select`, and `.sql()` (a SQL string, or Ibis compiled to DuckDB SQL).
  Not eligible: `.accumulate()` and `agg.reduce` (Python folds), and the incremental path (small by
  construction, so it stays local). `.aggregate()`'s comprehensive rebuild is a candidate for a second
  step; v1 sends only its input relation if that's simple, else keeps it local.
- **`dispatch(builder, out_pk)`**:
  1. Compile the builder's comprehensive SQL with each leaf replaced by the data plane's read of that
     Source on the object store, pinned to the run's freshness (`DataPlane.read_select(..., as_of=f)`,
     which with `plans/versioned-overwrite.md` is exact for overwrite tables too). This needs a builder
     seam that emits its own SQL with substituted leaves, instead of a per-engine compiler like Athena's.
  2. Run it on a MotherDuck connection, forced to execute remotely.
  3. Return the result as a relation: streamed back for small results, or written by MotherDuck to a
     scratch prefix and read from there for large ones.
  4. `conform` it, as for any engine. Any failure returns `None` and the Duck computes locally, counted on
     `duckstring_flock_dispatch_failures_total`, unchanged.
- **`estimate_rows`**: from the published sidecar stats where present, instead of counting each leaf.

### Default engine

`DUCKSTRING_FLOCK_ENGINE` unset currently means `athena`. It becomes: `motherduck` if a MotherDuck token
is configured, else `athena` if Athena is configured, else off. Explicit settings are unchanged.

### Getting Flock settings to every Duck (a gap found while planning)

Only Ducks on the Catchment's machine (`SubprocessLauncher`) and on Pool agents (`pool_launcher`) receive
the Pond's Flock mode, engine and OOM policy, as environment variables. Fargate and EC2 Ducks receive
neither those nor any engine credentials, so today the Flock can't dispatch from them at all.

Fix: carry the Pond's effective Flock settings, and the chosen engine's resolved credentials, in each
`begin_run` job instead of the environment. Jobs already travel over the authenticated Duck channel, so
this works the same for every launcher, keeps tokens out of task definitions and console-visible
commands, and picks up a rotated token on the next run without respawning the Duck. The Duck holds them
in memory for the run only. Drop the environment variables once every launcher uses the job.

### Docs

- The Flock moves out of the AWS guide into its own guide ("Offloading large recomputes"), with
  MotherDuck first and Athena second: setup (service accounts per size, cooldown, the S3 secret for
  MotherDuck), choosing per Pond, what's eligible, costs, and data leaving the user's account.
- `pond_toml.md` `[flock]`, `cli/duck.md` `--engine`, `environment.md` (Flock variables, the new default
  rule), `management_and_execution.md` (Ducks and Flocks).

## Spike first (needs a MotherDuck account)

- How to force remote execution for a query that reads the customer's bucket from an `md:` connection,
  and how MotherDuck gets read access to the bucket (a persistent `CREATE SECRET ... IN MOTHERDUCK` set up
  once by the user is the likely answer; check what can be scoped to one bucket or prefix).
- Result transfer: streamed Arrow throughput for a large result, against writing to a scratch prefix.
- Client version compatibility: which local DuckDB versions MotherDuck accepts, and what happens across
  a version boundary.
- Per-size memory, measured, so the docs can say which size suits which envelope.
- Cost of a typical dispatch on Pulse against Jumbo with a 1-minute cooldown.

## Tests

- `tests/test_flock_motherduck.py`: eligibility (what's sent, what stays local and why), leaf
  substitution and as-of pinning, the profile-to-secret mapping, the default-engine rule, and the
  fallback on any error. A fake MotherDuck connection keeps these offline, as `tests/flock_fake_engine.py`
  does for the seam.
- `tests/test_flock_motherduck_conformance.py`: the conformance gate, run against a real account (skipped
  without a token), mirroring `test_flock_athena_conformance.py`: every builder shape in the suite, local
  against MotherDuck, results compared.
- The job-carried settings: a Fargate-shaped Duck (no Flock environment) dispatches using the job's
  settings; a rotated secret is used on the next run.

## Effort

About three days: half a day of spike, a day and a half for the engine and the builder SQL seam, half a
day to move Flock settings into the job, half a day of docs.

## Sources

- [MotherDuck: Duckling sizes](https://motherduck.com/docs/about-motherduck/billing/duckling-sizes/)
- [MotherDuck: Pricing model](https://motherduck.com/docs/about-motherduck/billing/pricing/)
- [MotherDuck: Hypertenancy](https://motherduck.com/docs/concepts/hypertenancy/)

## As built (2026-10-03): Flock settings in the job

- `flock.job_settings(duck_cfg, environ, secret)` builds, on the Catchment, a flat environment mapping:
  the Pond's effective `flock_mode`/`flock_engine`/`oom_policy` as `DUCKSTRING_FLOCK_MODE`/`_ENGINE`/
  `_OOM_POLICY`, every other `DUCKSTRING_FLOCK_*` setting in the Catchment's environment (Athena's
  workgroup and so on, `MIN_ROWS`), and the engine's credentials. `_dispatch_begin_run` puts it on the job
  as `flock`.
- Credentials are declared by the engine class as `SECRETS` (names), resolved from the secret store
  (`credentials.secret_value`, new) else the Catchment's environment, and sent **only when the Pond's
  mode isn't `off`**. Athena declares none (it uses the Duck's IAM role). An engine the Catchment can't
  import declares none, so a `module:Class` engine only installed in Pond environments still works,
  without credentials from the Catchment. The MotherDuck engine will declare `MOTHERDUCK_TOKEN` (and its
  per-size profile names) here; its fallback to MotherDuck's own `motherduck_token` variable belongs in
  the engine.
- The Duck keeps the mapping per Run (`RunInputs.flock_for`), overlays it on its own environment and
  passes it to the Pond handle as `flock_env`; the builder hands it to `flock.enabled`/`comprehensive`,
  which thread it to `get_engine`, the mode, the row envelope and the OOM policy. The engine is built per
  terminal, so a rotated secret is used from the next run.
- The subprocess and Pool launchers no longer set the Flock variables. The Pool agent's generic `env`
  channel on its ensure job stays, empty. `DUCKSTRING_MEMORY_LIMIT` stays a Duck environment setting,
  since it describes the Duck's machine.
- Tests: `tests/test_flock_job.py`.

