# Pond environments: each Pond runs in its own Python environment

Status: **built** (2026-09-29): phase 1 (local Ducks + deploy-time discovery), then phases 2 and 3
(Pool and cloud Ducks) together, since one mechanism covers both. The object-store environment cache
sketched for phase 3 is deferred (see Phases).

## Problem

Every Duck, and the deploy-time discovery of a Pond's Ripples, runs in the Catchment's own Python
environment. So:

- A Pond that imports a package the Catchment lacks fails at run time, or worse, deploys with **zero
  Ripples**: `routes/deploy._discover_ripples` swallows the import error and returns `[]`.
- Two Ponds can't use different versions of the same package (two dbt versions, two pandas majors).
- A deployed Pond doesn't behave like the same Pond run locally, because the environment differs.
- Pond code is imported **inside the Catchment process** at deploy (Ripple discovery, column-lineage
  capture, dbt parsing). Import-time side effects, crashes and same-named modules affect the Catchment
  itself; `core.import_pond_module` evicts a Pond's modules after each import to contain the leakage.

A Pond is meant to be an isolated unit that happens to run on a Catchment. A deployment should behave
exactly as the Pond does locally.

## Decisions (agreed with the author, 2026-09-29)

1. **The Pond declares its environment; the Catchment builds it.** A Pond may contain a standard
   `pyproject.toml` (its dependencies) and a `uv.lock`. `pond.toml` stays Duckstring's own metadata.
   One format only: `requirements.txt` is not accepted (no lock, so not reproducible). Converting is
   `uv init --bare && uv add -r requirements.txt`.
2. **No `pyproject.toml` is fine**: the Pond runs in the default environment (the Catchment's own
   Python), which covers plain SQL Ponds at no cost.
3. **A `pyproject.toml` without a `uv.lock` is rejected at deploy** ("run `uv lock`"), and a stale lock
   too (`uv sync --locked`). Resolving at deploy time would make the deployed environment differ from
   the developer's. "Stale" is uv's definition: a dependency missing from the lock is stale, but a
   tightened specifier the locked version still satisfies is not.
4. **Environments are built once per content hash** and shared: `{root}/envs/{hash}/`, where the hash
   covers the lock file, `pyproject.toml`, the Duckstring requirement and the extras. Ponds with
   identical locks share one environment; an unchanged redeploy reuses it. uv's cache links packages
   rather than copying, so builds are fast (measured: ~7s cold for Duckstring's full dependency set,
   well under 1s to lock + sync a small Pond) and disk use is modest.
5. **The Catchment installs its own Duckstring into every environment**, overriding any pin in the
   Pond, since the Duck and the Catchment must speak the same protocol. From PyPI:
   `duckstring[extras]=={version}`. From a development checkout (an editable install, detected from the
   distribution's `direct_url.json`): the local source path, editable. Extras mirror what the Catchment
   itself has installed (`aws` when `s3fs`/`boto3` are importable, `lineage` when `sqlglot` is), so a Duck
   never lacks a dependency the Catchment relies on (the first real Fargate run failed on exactly that).
6. **uv is a core dependency** (a self-contained binary wheel on PyPI; `uv.find_uv_bin()` locates it).
7. **Deploy-time discovery always runs in a subprocess**, even for a Pond on the default environment:
   one code path, and the Catchment never imports Pond code again. `import_pond_module`'s eviction
   remains for the Duck and local runs, where one process hosts one Pond.
8. **Pools don't require a shared environment.** Each Duck on a Pool machine is its own process and can
   run its own environment; the Pool agent builds each distinct environment once, cached by hash.
   Requiring one environment per Pool would force Ponds sharing a Pool to upgrade dependencies in lock
   step, which is the coordination Duckstring exists to remove.

## Design

### `duckstring/environments.py`

- `pond_env(source_dir) -> EnvSpec | None`: `None` without a `pyproject.toml`; raises `EnvError` for a
  missing `uv.lock`.
- `env_hash(spec)`: sha256 over the lock, `pyproject.toml`, the interpreter, and the Duckstring
  requirement (which includes the extras).
- **Interpreter**: the Catchment's own `sys.executable`, unless the Pond has a `.python-version` (uv then
  honours it, installing that Python if needed). Without this uv picks the newest Python it finds, which
  on a developer machine differed from the Catchment's.
- `ensure_env(root, source_dir) -> Path`: the Python interpreter to use. Default environment:
  `sys.executable`. Otherwise builds `{root}/envs/{hash}/` if it isn't complete, in place under a file
  lock (concurrent deploys of the same lock build once), finishing with a `.complete` marker; a build
  without the marker is removed and redone. Build = `uv sync --locked --no-install-project --no-dev` with
  `UV_PROJECT_ENVIRONMENT` pointed at the target, then `uv pip install` of the Catchment's Duckstring. A
  failure raises `EnvError` carrying uv's output.
- The Pond's deployed source directory records its environment in `.duckstring_env` (the hash), so a
  launcher finds the interpreter without the database: `python_for(root, source_path)`.
- Old environments are not garbage-collected in phase 1 (`duckstring catchment gc` is a follow-up).

### Discovery subprocess: `python -m duckstring.discover IN OUT`

- **In** (a JSON file): the Pond's source directory, and the Source schemas the column-lineage capture
  needs (`{"source.table": [columns]}`, read from `pond_version_schema` for the declared Sources, since
  the subprocess has no database).
- **Out** (a JSON file, never stdout, which Pond code may print to):
  `{"ok": true, "ripples": [{"name", "parents": [names], "always_run"}], "lineage": [[table, column,
  kind, src_ref, src_column], ...]}`, or `{"ok": false, "error", "traceback"}`.
- Covers `@ripple` discovery, dbt-mode parsing (`dbt` must then be in the Pond's environment, which is
  the point), and column-lineage capture (still best-effort: a capture failure yields no lineage rows,
  never a failed deploy).
- **An import failure fails the deploy** (422, with the error and traceback), instead of registering
  zero Ripples.
- A timeout (`DUCKSTRING_DISCOVER_TIMEOUT`, default 300s) so a hanging import can't hang a deploy.
- Ripple rows come back with names for `func` and `parents`, the shape dbt mode already uses, so
  `_register`/`_incoming_topology` need no change.

### Deploy route

The route runs the environment build and discovery in a worker thread (`asyncio.to_thread`): the
handler is `async`, and a long build must not block the Catchment's event loop. The CLI's deploy
timeout grows to cover a first build.

### Launchers

- `SubprocessLauncher.ensure` spawns `python_for(root, source_path) -m duckstring.duck`, with
  `DUCKSTRING_IN_POND_ENV=1` when that is a built Pond environment.
- Every other Duck (a Pool agent's child, a Fargate task, an EC2 instance) starts with its machine's
  own Python and **switches itself** (`duck/__main__._use_pond_env`, `environments.duck_python`): after
  fetching its source artifact it builds the Pond's environment under its own root
  (`ensure_env(..., check_lock=False)`) and `os.execv`s into that environment's Python with the same
  arguments, setting `DUCKSTRING_IN_POND_ENV` so the new process doesn't repeat it. No launcher changes:
  the Pool agent only strips the marker from its children's environment.
- While building, the Duck posts a `booting` event every 15 s (any event counts as contact; the Driver
  ignores the kind), so a long build isn't judged a silent Duck. A failed build is posted as
  `pond_failed` ("Building the Pond's environment failed: …", uv's output as the traceback) and the Duck
  exits, so the reason shows in the UI rather than as a dead Duck.
- **The lock is checked once, at deploy** (`--locked` on the Catchment). A Duck elsewhere installs it
  `--frozen`. Every build passes `--no-install-package duckstring` (the machine's own is installed next).
  Both are needed for a lock made against a local Duckstring checkout: `--locked` re-reads the locked
  Duckstring's source to check freshness, and that path exists only on the developer's machine. Such a
  lock still can't deploy to a Catchment on another machine, which is correct.
- **Which Duckstring** (`duckstring_requirement`): the machine's own install, from `direct_url.json`:
  an editable checkout's path, the wheel file it was installed from if that file still exists, a VCS
  URL at its commit, or an archive URL; otherwise (no direct URL, or the wheel is gone) the same released
  version from the index. The `Dockerfile` now keeps its wheel at `/opt/duckstring/` so an image built
  from an unreleased wheel can install that same build into Pond environments.
- A machine with no `HOME` (EC2 userdata runs under cloud-init) gets `UV_CACHE_DIR={root}/uv-cache`.

### Local runs

`duckstring pond run` and `pond hydrate` re-run themselves under the Pond's `.venv/bin/python` when the
Pond has a `pyproject.toml` and a `.venv` (created by `uv sync`) and the current interpreter isn't it.
If that environment lacks Duckstring, the message says to `uv add duckstring`.

## Phases

1. **Local Ducks and deploy-time discovery.** Everything above. Tests: environment spec and hashing,
   a real build of a tiny Pond (gated when uv or the network is unavailable), discovery success/failure
   (including the previously silent import error), lineage over the subprocess, the Duck spawned in the
   environment's Python, and local re-exec.
2. **Pool agents** and 3. **Cloud Ducks (Fargate, EC2)**: built together as the Duck switching itself
   (see Launchers), rather than the agent building environments, since a Fargate or EC2 Duck has no
   agent and the same code then serves all three. A Pool machine keeps built environments in its root
   for later Ducks; a cloud Duck builds on every cold start.
   Tests: `test_runtime.py::test_a_pool_duck_builds_its_ponds_environment` (a local Pool agent with its
   own root: artifact fetch → build → switch → run), unit tests for `duck_python`, the requirement
   fallbacks and the failed-build report. Verified by hand in the repository's Docker image as the
   non-root user: the requirement resolves to the kept wheel, a cold build (empty uv cache, Duckstring's
   own dependencies plus a small local package) took 12.6 s, the Pond's code loads, reuse is instant.
   **Deferred: the object-store cache** (`{data_root}/_envs/{hash}.tar`). It would save roughly that
   build time on a start that already includes an image pull, a venv isn't relocatable across paths or
   interpreters (the key would have to include both), and it doesn't help a Duck without index access
   (something with access must build it first). Revisit if a real Pond's cold build dominates its Fargate
   start.

## Notes from phase 1

- Building a Pond environment resolves Duckstring's own dependencies afresh, so it is the first place a
  new upstream release bites. It found that pyiceberg 0.12 made `load_view`/`register_view` abstract on
  `MetastoreCatalog`, which broke `FileCatalog` for any fresh install (stubbed; the suite passes on the
  latest dependencies). A CI job on latest-resolved dependencies would catch this class earlier.
- `import_pond_module` now clears the Ripple/Puddle registries first. The in-process deploy discovery
  used to drain leftovers by accident; without it a test could see another test's Ripples.

## Follow-ups

- Garbage collection of environments no deployed Pond references.
- Showing a Pond's environment (hash, Python version, key packages) in `/api/status` and the UI.
- A per-Pond Python version, from `requires-python` (uv can install interpreters); phase 1 uses whatever
  interpreter uv selects for the lock.
