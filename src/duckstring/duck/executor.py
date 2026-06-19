"""RippleExecutor — runs a Pond's Ripple functions in a thread pool.

Owns ripple loading, execution against the Pond's DuckDB registry, and the atomic Parquet export for
cross-Pond consumption. Each Duck has one executor bound to its Pond's deployed source. Execution is
opaque to :class:`~duckstring.duck.core.DuckCore`, which only needs "launch this Ripple" and "tell me
when it finished".
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ..catchment.registry import pond_registry_path

_import_lock = threading.Lock()


def load_topology(source_dir: Path) -> dict[str, list[str]]:
    """Build the intra-Pond ``{ripple_name: [parent_names]}`` graph by importing the deployed
    ripples entrypoint and reading the registered ripples (the Duck owns its own code)."""
    ripples = _import_ripples(source_dir)
    func_to_name = {r["func"]: r["name"] for r in ripples}
    return {
        r["name"]: [func_to_name[p] for p in r["parents"] if p in func_to_name]
        for r in ripples
    }


def _import_ripples(source_dir: Path) -> list[dict]:
    from ..core import collect_ripples, import_pond_module, pond_entrypoints, read_pond_toml

    ripples_entry, _ = pond_entrypoints(read_pond_toml(source_dir))
    import_pond_module(source_dir, ripples_entry)
    return collect_ripples()


def _load_ripple(source_path: str, root: str, ripple_name: str):
    """Load ``ripple_name``'s function from the deployed code. Importing here (lazily, per run) keeps
    executor construction free of the Pond's code, so an executor can be stood up for export-only paths
    that never import a ripple."""
    from ..core import import_pond_module, pond_entrypoints, read_pond_toml

    source_dir = Path(root) / source_path
    with _import_lock:
        ripples_entry, _ = pond_entrypoints(read_pond_toml(source_dir))
        mod = import_pond_module(source_dir, ripples_entry)
        return getattr(mod, ripple_name)


def _run_ripple(
    func, pond_name: str, version: str, con, root_str: str,
    source_majors: dict[str, int], f: datetime | None, previous_f: datetime | None,
) -> None:
    from ..core import Pond

    # ``con`` is a cursor off the executor's single shared registry instance (see RippleExecutor).
    # Ripples run concurrently on pool threads, each with its own cursor — they share the one instance,
    # so they coexist without the "file handle conflict" two separate connect()s to the same file raise.
    try:
        func(Pond(
            name=pond_name, version=version, con=con, root=Path(root_str),
            source_majors=source_majors, f=f, previous_f=previous_f,
        ))
    finally:
        con.close()


def _export_data(con, registry_path: Path, f: datetime | None, contract=None) -> dict | None:
    from ..dataplane import get_data_plane
    from ..schema_contract import ContractViolation, contract_violations, extract_schema

    data_dir = registry_path.parent / "data"

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
            raise ContractViolation("; ".join(violations))
        get_data_plane().export(con, data_dir, mode="overwrite", f=f)
        return schema
    finally:
        con.close()


class RippleExecutor:
    def __init__(self, pond_name: str, major: int, version: str, source_path: str, root: Path, max_workers: int = 8):
        import duckdb

        from ..core import read_pond_toml
        from ..keys import spec_major

        self.pond_name = pond_name
        self.major = major
        self.version = version
        self.source_path = source_path
        self.root = root
        self.registry_path = pond_registry_path(root, pond_name, major)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        # ONE registry instance for the Duck's life: ripples (and the export) each run on a `.cursor()`
        # off it. Separate `connect()`s to the same file in one process raise a "file handle conflict"
        # (a Binder error, not a transient lock) the moment two overlap — single instance avoids it.
        self._registry = duckdb.connect(str(self.registry_path))
        self._cursor_lock = threading.Lock()
        # Which major line of each Source this Pond's reads resolve to (its pond.toml pins).
        sources = read_pond_toml(root / source_path).get("sources", {})
        self.source_majors = {sname: spec_major(spec) for sname, spec in sources.items()}
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def _cursor(self):
        """A fresh connection sharing the one registry instance. Cursor creation is serialised; the
        cursors themselves run concurrently."""
        with self._cursor_lock:
            return self._registry.cursor()

    def submit(self, ripple_name: str, f: datetime | None, previous_f: datetime | None, on_done, on_error):
        """Load and run ``ripple_name`` at freshness ``f`` (exposed to the ripple as ``pond.f``, with
        the prior run's freshness as ``pond.previous_f``); call ``on_done(name, started_at,
        finished_at)`` on success and ``on_error(name, exc, started_at, finished_at)`` on failure
        (timings wall-clock UTC, for the run-history duration; both fire on a pool thread)."""
        timing: dict[str, datetime] = {}

        def _task():
            timing["started"] = datetime.now(timezone.utc)
            func = _load_ripple(self.source_path, str(self.root), ripple_name)
            _run_ripple(
                func, self.pond_name, self.version, self._cursor(), str(self.root),
                self.source_majors, f, previous_f,
            )

        fut = self._pool.submit(_task)

        def _cb(f):
            exc = f.exception()
            finished = datetime.now(timezone.utc)
            started = timing.get("started", finished)
            if exc:
                on_error(ripple_name, exc, started, finished)
            else:
                on_done(ripple_name, started, finished)

        fut.add_done_callback(_cb)
        return fut

    def export(self, f: datetime | None = None, contract=None) -> dict | None:
        """Publish the Pond's tables for cross-Pond consumption via the data plane, stamped with the
        run's freshness ``f`` (recorded by snapshotting backends). ``contract`` is the major line's
        additive contract — a violation raises :class:`ContractViolation` *before* publishing (last-good
        is left intact). Returns the published output schema (for the Catchment to capture)."""
        return _export_data(self._cursor(), self.registry_path, f, contract)

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

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
        self._registry.close()
