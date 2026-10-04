"""RippleExecutor — runs a Pond's Ripple functions in a thread pool.

Owns ripple loading, execution against the Pond's DuckDB registry, and the atomic Parquet export for
cross-Pond consumption. Each Duck has one executor bound to its Pond's deployed source. Execution is
opaque to :class:`~duckstring.duck.core.DuckCore`, which only needs "launch this Ripple" and "tell me
when it finished".
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ..catchment.registry import pond_data_dir, pond_major_dir, pond_registry_path
from ..dataplane import KEEP_ALL
from ..objects import STAGING_DIR

_import_lock = threading.Lock()


def load_topology(source_dir: Path) -> dict[str, list[str]]:
    """Build the intra-Pond ``{ripple_name: [parent_names]}`` graph. For a normal Pond this collects its
    Python and SQL Ripples (:func:`duckstring.core.load_ripples`); for a **dbt-mode** Pond it parses the
    dbt project and reads the model graph (a model = a Ripple). The Duck owns its own code either way."""
    from ..core import load_ripples, read_pond_toml
    from ..dbt_mode import dbt_project_subpath

    info = read_pond_toml(source_dir)
    if dbt_project_subpath(info):
        return _dbt_topology(source_dir, info)
    with _import_lock:
        return load_ripples(source_dir, info).topology()


def _dbt_topology(source_dir: Path, info: dict) -> dict[str, list[str]]:
    import tempfile

    from ..dbt_mode import manifest_topology, parse_manifest, project_dir, write_profile

    with tempfile.TemporaryDirectory() as tmp:
        profiles = write_profile(Path(tmp), ":memory:")  # topology parse touches no registry
        manifest = parse_manifest(project_dir(source_dir, info), profiles)
    return manifest_topology(manifest)



def _load_ripple(source_path: str, root: str, ripple_name: str):
    """Load ``ripple_name``'s function (Python or SQL) from the deployed code, with the Pond's Ripples
    around it (:func:`duckstring.core.load_ripples`). Importing here (lazily, per run) keeps executor
    construction free of the Pond's code, so an executor can be stood up for export-only paths that never
    import a ripple. Returns ``(func, pond_ripples)``."""
    from ..core import load_ripples

    source_dir = Path(root) / source_path
    with _import_lock:
        pond = load_ripples(source_dir)
    return pond.by_name[ripple_name]["func"], pond


def _run_ripple(
    func, pond_name: str, version: str, con, root_str: str,
    source_majors: dict[str, int], f: datetime | None, previous_f: datetime | None,
    data_root: str | None = None, sources_changed: bool = True, skip_sink=None,
    staging_dir=None, own_data_dir=None, source_f: dict[str, str] | None = None,
    flock: dict[str, str] | None = None, scope: dict | None = None,
) -> dict:
    from ..core import Pond

    # The run's Flock settings (from its begin_run job) over the Duck's own environment, so a Duck whose
    # launcher passed no environment (Fargate, EC2) still has the Pond's posture, engine and credentials.
    flock_env = {**os.environ, **flock} if flock else None

    # ``con`` is a cursor off the executor's single shared registry instance (see RippleExecutor).
    # Ripples run concurrently on pool threads, each with its own cursor — they share the one instance,
    # so they coexist without the "file handle conflict" two separate connect()s to the same file raise.
    pond = Pond(
        name=pond_name, version=version, con=con, root=Path(root_str),
        source_majors=source_majors, source_f=source_f, f=f, previous_f=previous_f, data_root=data_root,
        sources_changed=sources_changed, skip_sink=skip_sink,
        staging_dir=staging_dir, own_data_dir=own_data_dir,
        # Flock is a Pond-level posture (not per-Ripple). flock.comprehensive still applies
        # engine-eligibility and the OOM fail-up.
        flock=(flock_env or os.environ).get("DUCKSTRING_FLOCK_MODE"), flock_env=flock_env,
        # The Ripple's place in the Pond: its static tables, and what the own-table read check needs.
        **(scope or {}),
    )
    try:
        func(pond)
        return pond.take_lineage()  # the observed reads/writes this Ripple made (plans/lineage.md)
    finally:
        con.close()


def _export_data(con, data_dir, f: datetime | None, contract=None, retain_from=KEEP_ALL) -> dict | None:
    from ..dataplane import get_data_plane
    from ..schema_contract import CONTRACT_PREFIX, ContractViolation, contract_violations, extract_schema

    # ``con`` is a cursor off the shared instance: the export reads a consistent MVCC snapshot and shares
    # the ripples' configuration, so it neither clashes with their open connections nor conflicts on the
    # file handle the way a separate connect() to the same file would. The data plane owns the publish
    # format (Parquet today); ``f`` is the run's freshness, recorded by backends that snapshot.
    try:
        schema = extract_schema(con)
        # The contract gate: vet the output BEFORE publishing. A violation aborts the publish, so the
        # live tables keep last-good data; the Catchment fails the Pond and blocks downstream.
        violations = contract_violations(schema, contract)
        if violations:
            # The stable prefix is the failure's machine-readable sub-reason: the Catchment's status
            # derives failure_kind="contract" from it (survives restarts — no extra state to persist).
            raise ContractViolation(f"{CONTRACT_PREFIX}{'; '.join(violations)}")
        get_data_plane().export(con, data_dir, mode="overwrite", f=f, retain_from=retain_from)
        return schema
    finally:
        con.close()


# DuckDB errors at open that mean the registry file can't be used by this DuckDB: written by a newer
# storage version (a Pond environment can run a different DuckDB from the one that wrote it), not a DuckDB
# file, or corrupt. Matched on the message, since they are all IOException; anything else (a lock held by
# another process above all) is re-raised, because setting aside a file someone holds would be destructive.
_UNREADABLE_MARKERS = (
    "not a valid duckdb database file",
    "trying to read a database file with version number",
    "corrupt database file",
    "checksum",
    "replaying wal",
)


def _is_unreadable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "lock" not in msg and any(m in msg for m in _UNREADABLE_MARKERS)


def open_registry(path: Path):
    """Open a Pond's registry, returning ``(connection, recovery)``: ``recovery`` is ``"missing"`` when
    the file didn't exist, ``"unreadable"`` when it existed but this DuckDB can't open it (it is renamed
    aside with its WAL, kept for inspection, and a fresh registry is created), else ``None``. Either
    recovery means the caller should rebuild the registry from published state."""
    import duckdb

    if not path.exists():
        return duckdb.connect(str(path)), "missing"
    try:
        return duckdb.connect(str(path)), None
    except duckdb.Error as exc:
        if not _is_unreadable(exc):
            raise
        reason = str(exc).splitlines()[0]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aside = path.with_name(f"{path.name}.unreadable-{stamp}")
    path.rename(aside)
    wal = path.with_name(f"{path.name}.wal")
    if wal.exists():
        wal.rename(aside.with_name(f"{aside.name}.wal"))
    print(f"[executor] registry {path} is unreadable ({reason}); moved it to {aside.name} and rebuilding",
          flush=True)
    return duckdb.connect(str(path)), "unreadable"


class RunInputs:
    """What each ``begin_run`` job tells the Duck about its Run's inputs, kept per Run freshness so
    pipelined Runs don't share them: the Sources' published freshness (the version pins its reads use),
    the Catchment's ``retain_from`` for this line's own overwrite versions, and the Pond's Flock settings
    (``flock.job_settings``). Shared by both executors."""

    def _inputs(self) -> dict:
        if not hasattr(self, "_run_inputs"):
            self._run_inputs: dict[datetime, dict] = {}
        return self._run_inputs

    def begin_run_inputs(self, f: datetime, source_f: dict[str, str] | None, retain_from=KEEP_ALL,
                         force: bool = False, flock: dict[str, str] | None = None) -> None:
        """Record Run ``f``'s inputs. The first job for ``f`` wins (a re-dispatch after a Catchment restart
        must not move a Run's pins under its running Ripples), unless ``force`` restarts the Run.
        ``retain_from`` is a datetime, ``None`` (keep only the newest version) or ``KEEP_ALL`` (no job
        value: prune nothing)."""
        self.source_f = source_f or {}  # the latest job's view (fallback for a Run with no recorded inputs)
        self.flock = flock  # likewise
        inputs = self._inputs()
        if force or f not in inputs:
            inputs[f] = {"source_f": dict(source_f or {}), "retain_from": retain_from, "flock": flock}

    def source_f_for(self, f: datetime | None) -> dict[str, str]:
        rec = self._inputs().get(f)
        return rec["source_f"] if rec is not None else self.source_f

    def flock_for(self, f: datetime | None) -> dict[str, str] | None:
        """Run ``f``'s Flock settings from its job, or ``None`` (no job carried any: the environment's)."""
        rec = self._inputs().get(f)
        return rec["flock"] if rec is not None else getattr(self, "flock", None)

    def take_retain_from(self, f: datetime | None):
        """Run ``f``'s retention bound, consumed by its publish (``KEEP_ALL`` if no job recorded one)."""
        rec = self._inputs().pop(f, None)
        return rec["retain_from"] if rec is not None else KEEP_ALL

    def prune(self, retain_from) -> int:
        """Trim superseded overwrite versions now (the Catchment's ``shutdown`` job, sent when the Pond goes
        idle). See :func:`duckstring.dataplane.prune_versions`."""
        from ..dataplane import prune_versions

        return prune_versions(self.own_data_dir, retain_from)


class RippleExecutor(RunInputs):
    def __init__(self, pond_name: str, major: int, version: str, source_path: str, root: Path,
                 max_workers: int = 8, data_root: str | None = None, persist_root: str | None = None):
        from ..core import read_pond_toml
        from ..keys import spec_major

        self.pond_name = pond_name
        self.major = major
        self.version = version
        self.source_path = source_path
        self.root = root
        self.data_root = data_root
        self.registry_path = pond_registry_path(root, pond_name, major)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        # Object (non-tabular output) staging + own published location — see objects.py.
        self.staging_dir = pond_major_dir(root, pond_name, major) / STAGING_DIR
        # LOCAL-FIRST PUBLISH (plans/persist.md): with `persist_root` set (a Duck on the Catchment Pool,
        # cloud enabled) the run publishes to the LOCAL layout — the fast handoff co-located Sinks read —
        # and `persist()` asynchronously mirrors it to the durable layer afterwards. Without it (no cloud,
        # or a remote-Pool Duck whose local disk nothing else can reach) the publish target is data_root
        # directly, exactly as before.
        if persist_root:
            self.own_data_dir = pond_data_dir(root, pond_name, major, None)
            self.persist_dir = pond_data_dir(root, pond_name, major, persist_root)
        else:
            self.own_data_dir = pond_data_dir(root, pond_name, major, data_root)
            self.persist_dir = None
        # Registry-loss recovery: the registry FILE is gone (host loss / migration / scale-to-zero) or this
        # DuckDB can't read it (a newer storage version, corruption; set aside by open_registry), but
        # published state survives → rebuild the registry from it (tiers + meta + the Extension-1 agg/acc
        # snapshots), so the next run resumes *incrementally* instead of re-bootstrapping. Deliberately
        # keyed on the file, NOT on an empty registry: a Refresh `wipe()` empties the file in place, and
        # re-hydrating behind a wipe would defeat the cold rebuild.
        # ONE registry instance for the Duck's life: ripples (and the export) each run on a `.cursor()`
        # off it. Separate `connect()`s to the same file in one process raise a "file handle conflict"
        # (a Binder error, not a transient lock) the moment two overlap — single instance avoids it.
        self._registry, recovery = open_registry(self.registry_path)
        recover = recovery is not None
        # A containerised Duck must hit DuckDB's own limit (a catchable OutOfMemoryException,
        # with disk spill first) rather than the kernel's cgroup OOM-kill: the launcher sets
        # DUCKSTRING_MEMORY_LIMIT to ~80% of the pod cap. Absent env => DuckDB defaults, no change.
        mem_limit = os.environ.get("DUCKSTRING_MEMORY_LIMIT")
        if mem_limit:
            spill = Path(self.root) / "tmp"
            spill.mkdir(parents=True, exist_ok=True)
            self._registry.execute(f"SET memory_limit = '{mem_limit}'")
            self._registry.execute(f"SET temp_directory = '{spill}'")
        # Configure the data-plane's object-store credentials on the registry INSTANCE up front (a DuckDB
        # SECRET is instance-wide, shared by every cursor), so registry-loss hydration below, cross-Pond
        # foreign reads, and the export can all reach S3/GCS. A no-op for a local data root. Without this,
        # the first S3 read (typically hydration) fails with a 403 "no credentials provided".
        self.own_data_dir.duckdb_setup(self._registry)
        if recover:
            from ..dataplane import hydrate_registry, restore_tree

            # Hydrate from the local publish (co-located, cheap). A true box loss (local publish gone too)
            # first restores the local publish from the durable persist layer (the whole point of
            # always-persist): the registry's views read it, and Ponds on this machine read it too.
            where = "published state"
            if self.persist_dir is not None and not self.own_data_dir.exists("_trickle.json") \
                    and self.persist_dir.exists("_trickle.json"):
                restore_tree(self.persist_dir, self.own_data_dir)
                where = "persist layer"
            hydrated = hydrate_registry(self._registry, self.own_data_dir)
            if hydrated:
                print(f"[executor] registry file was {recovery} — hydrated {len(hydrated)} table(s) "
                      f"from the {where}: {', '.join(hydrated)}", flush=True)
        self._cursor_lock = threading.Lock()
        # Which major line of each Source this Pond's reads resolve to (its pond.toml pins).
        sources = read_pond_toml(root / source_path).get("sources", {})
        self.source_majors = {sname: spec_major(spec) for sname, spec in sources.items()}
        # What the Catchment says each Source has published ({name: iso}) — carried on the begin_run job
        # and used to reject a stale LOCAL publish left behind by a Source that moved to a remote Pool.
        # Empty until a job supplies it; then the resolve is only ever more correct, never less.
        self.source_f: dict[str, str] = {}
        # {table: the Ripple that writes it}, learned from each completed Ripple's lineage (a Python
        # Ripple's tables aren't known until it runs). Backs Pond's own-table read check.
        self.table_writers: dict[str, str] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def _cursor(self):
        """A fresh connection sharing the one registry instance. Cursor creation is serialised; the
        cursors themselves run concurrently."""
        with self._cursor_lock:
            return self._registry.cursor()

    def submit(self, ripple_name: str, f: datetime | None, previous_f: datetime | None, on_done, on_error,
               sources_changed: bool = True, skip_sink=None):
        """Load and run ``ripple_name`` at freshness ``f`` (exposed to the ripple as ``pond.f``, with
        the prior run's freshness as ``pond.previous_f``); call ``on_done(name, started_at,
        finished_at, lineage)`` on success — ``lineage`` is the Ripple's observed reads/writes
        (plans/lineage.md) — and ``on_error(name, exc, started_at, finished_at)`` on failure
        (timings wall-clock UTC, for the run-history duration; both fire on a pool thread).
        ``sources_changed``/``skip_sink`` back ``pond.sources_changed()`` / ``pond.skip()``."""
        timing: dict = {}

        def _task():
            timing["started"] = datetime.now(timezone.utc)
            func, pond = _load_ripple(self.source_path, str(self.root), ripple_name)
            # Over-envelope offload happens inside the ripple, at the pond.trickle(...) terminals
            # (the Flock seam — duckstring.flock, env-gated, engine-pluggable). The executor just
            # runs the ripple classically; the terminal hook decides local-vs-Flock per output.
            timing["lineage"] = _run_ripple(
                func, self.pond_name, self.version, self._cursor(), str(self.root),
                self.source_majors, f, previous_f, self.data_root,
                sources_changed=sources_changed, skip_sink=skip_sink, source_f=self.source_f_for(f),
                flock=self.flock_for(f),
                staging_dir=self.staging_dir, own_data_dir=self.own_data_dir,
                scope={"static_tables": pond.statics, "ripple": ripple_name,
                       "ancestors": pond.ancestors(ripple_name),
                       "table_writers": {**self.table_writers, **pond.sql_writers()}},
            )
            # Learn which Ripple writes which table, for the own-table read check in later Ripples.
            for table in (timing["lineage"] or {}).get("writes", ()):
                self.table_writers.setdefault(table, ripple_name)

        fut = self._pool.submit(_task)

        def _cb(f):
            exc = f.exception()
            finished = datetime.now(timezone.utc)
            started = timing.get("started", finished)
            if exc:
                on_error(ripple_name, exc, started, finished)
            else:
                on_done(ripple_name, started, finished, timing.get("lineage"))

        fut.add_done_callback(_cb)
        return fut

    def export(self, f: datetime | None = None, contract=None) -> dict | None:
        """Publish the Pond's tables for cross-Pond consumption via the data plane, stamped with the
        run's freshness ``f`` (recorded by snapshotting backends). ``contract`` is the major line's
        additive contract — a violation raises :class:`ContractViolation` *before* publishing (last-good
        is left intact). Returns the published output schema (for the Catchment to capture)."""
        from ..objects import commit_objects

        schema = _export_data(self._cursor(), self.own_data_dir, f, contract, self.take_retain_from(f))
        # Objects commit only after the table publish passed the contract gate — a failed run leaves the
        # last-good Object intact (the staged writes are discarded on the next run / wipe).
        commit_objects(self.staging_dir, self.own_data_dir, f)
        return schema

    def persist(self) -> int:
        """Mirror the locally-published output to the durable persist layer (plans/persist.md) — the
        async step behind ``persisted_f``. Reconciles by file name (parts idempotent, wholesale re-upload,
        prunes what local no longer holds); replay-safe. No-op (0) when there is no persist layer.
        Runs off the serve loop — it touches only published files, never the registry."""
        if self.persist_dir is None:
            return 0
        from ..dataplane import persist_tree

        return persist_tree(self.own_data_dir, self.persist_dir)

    def wipe(self) -> None:
        """Drop every table in the Pond's registry — a Refresh's cold reset. The next run then reads its
        Sources in full (``previous_f = NEVER``) and rebuilds from scratch: a Trickle re-bootstraps (clean
        main + empty changelog + floor at this run's freshness), so downstream coverage-misses and reloads.
        The published snapshot is untouched until the rebuild re-exports."""
        from ..core import retry_on_lock

        def _drop() -> None:
            cur = self._cursor()
            try:
                # Views first (a view may depend on a table), then tables. SHOW TABLES lists both, and a
                # registry can hold leftover scratch views from a Trickle write (`relation.create_view`).
                for (v,) in cur.execute(
                    "SELECT view_name FROM duckdb_views() WHERE schema_name = 'main' AND NOT internal"
                ).fetchall():
                    cur.execute(f'DROP VIEW IF EXISTS "{v}"')
                for (t,) in cur.execute(
                    "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'"
                ).fetchall():
                    cur.execute(f'DROP TABLE IF EXISTS "{t}"')
            finally:
                cur.close()

        retry_on_lock(_drop)
        import shutil

        shutil.rmtree(self.staging_dir, ignore_errors=True)  # discard any uncommitted staged Objects

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
        self._registry.close()
