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


def _load_ripple_func(source_path: str, root: str, ripple_name: str):
    from ..core import collect_ripples, import_pond_module, pond_entrypoints, read_pond_toml

    source_dir = Path(root) / source_path
    with _import_lock:
        ripples_entry, _ = pond_entrypoints(read_pond_toml(source_dir))
        mod = import_pond_module(source_dir, ripples_entry)
        collect_ripples()
        return getattr(mod, ripple_name)


def _run_ripple(func, pond_name: str, version: str, registry_path_str: str, root_str: str) -> None:
    import duckdb

    from ..core import Pond, retry_on_lock

    # Retry only the connect (a transient lock from a concurrent reader/writer); the ripple body runs
    # exactly once. Ripples in a Pond share one registry file — concurrent read-write connections to it
    # are fine (each writes its own table); only the connect can momentarily clash.
    registry = retry_on_lock(lambda: duckdb.connect(registry_path_str))
    try:
        func(Pond(name=pond_name, version=version, con=registry, root=Path(root_str)))
    finally:
        registry.close()


def _export_parquet(registry_path: Path) -> None:
    import duckdb

    from ..core import retry_on_lock

    data_dir = registry_path.parent / "data"
    data_dir.mkdir(exist_ok=True)

    def _export() -> None:
        # Read-write (NOT read_only): a read_only connection clashes with the pipelined ripples' open
        # read-write connections ("different configuration than existing connections"). Same-config
        # connections coexist; the COPY reads a consistent MVCC snapshot.
        con = duckdb.connect(str(registry_path))
        try:
            for (table,) in con.execute("SHOW TABLES").fetchall():
                dest = data_dir / f"{table}.parquet"
                tmp = data_dir / f"{table}.parquet.tmp"
                con.execute(f'COPY "{table}" TO \'{tmp}\' (FORMAT PARQUET)')
                tmp.replace(dest)
        finally:
            con.close()

    retry_on_lock(_export)


class RippleExecutor:
    def __init__(self, pond_name: str, version: str, source_path: str, root: Path, max_workers: int = 8):
        self.pond_name = pond_name
        self.version = version
        self.source_path = source_path
        self.root = root
        self.registry_path = pond_registry_path(root, pond_name)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, ripple_name: str, on_done, on_error):
        """Load and run ``ripple_name``; call ``on_done(name, started_at, finished_at)`` on success and
        ``on_error(name, exc, started_at, finished_at)`` on failure (timings wall-clock UTC, for the
        run-history duration; both fire on a pool thread)."""
        timing: dict[str, datetime] = {}

        def _task():
            timing["started"] = datetime.now(timezone.utc)
            func = _load_ripple_func(self.source_path, str(self.root), ripple_name)
            _run_ripple(func, self.pond_name, self.version, str(self.registry_path), str(self.root))

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

    def export(self) -> None:
        """Export the Pond's tables to Parquet for cross-Pond consumption (atomic tmp+replace)."""
        _export_parquet(self.registry_path)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
