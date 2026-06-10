from __future__ import annotations

import time

_RIPPLES: list[dict] = []


def retry_on_lock(fn, attempts: int = 12, base: float = 0.05):
    """Run ``fn``, retrying on transient DuckDB lock/conflict errors so concurrent writers *queue*
    (back off and retry) rather than crashing. Covers the catalog write-write conflict, the read-only/
    read-write config clash, and the cross-process file lock. Re-raises after the last attempt."""
    import duckdb

    for i in range(attempts):
        try:
            return fn()
        except (duckdb.TransactionException, duckdb.IOException, duckdb.ConnectionException):
            if i == attempts - 1:
                raise
            time.sleep(min(base * (2**i), 0.5))


def ripple(func=None, *, parents=None, name=None):
    """Decorator that registers a function as a Ripple in a Pond.

    Usage:
        @ripple
        def load(pond): ...

        @ripple(parents=[load])
        def clean(pond): ...
    """
    if func is not None:
        # Called as @ripple without arguments
        _RIPPLES.append({"func": func, "name": name or func.__name__, "parents": parents or []})
        return func

    # Called as @ripple(...) with arguments
    def decorator(f):
        _RIPPLES.append({"func": f, "name": name or f.__name__, "parents": parents or []})
        return f

    return decorator


def collect_ripples() -> list[dict]:
    """Drain and return the current ripple registry. Used by the catchment at deploy time."""
    result = list(_RIPPLES)
    _RIPPLES.clear()
    return result


class Catchment:
    # TODO: client-side handle for communicating with the catchment server during execution
    pass


class Pond:
    def __init__(self, name: str, version: str, con, root) -> None:
        self.name = name
        self.version = version
        self.con = con
        self.root = root

    def write_table(self, name: str, relation) -> None:
        tmp = f"__tmp_{name}"

        def _write() -> None:
            self.con.execute("BEGIN TRANSACTION")
            try:
                self.con.execute(f'DROP TABLE IF EXISTS "{tmp}"')
                relation.create(f'"{tmp}"')
                self.con.execute(f'DROP TABLE IF EXISTS "{name}"')
                self.con.execute(f'ALTER TABLE "{tmp}" RENAME TO "{name}"')
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")  # release the txn so a retry starts clean
                raise

        retry_on_lock(_write)  # a concurrent write conflict queues + retries rather than failing

    def read_table(self, ref: str):
        if "." in ref:
            source_pond, table = ref.split(".", 1)
            if source_pond != self.name:
                from pathlib import Path as _Path
                parquet = _Path(self.root) / "ponds" / source_pond / "data" / f"{table}.parquet"
                if not parquet.exists():
                    raise FileNotFoundError(
                        f"No exported data found for '{source_pond}.{table}' — "
                        f"has {source_pond} completed a successful run?"
                    )
                return self.con.sql(f"SELECT * FROM read_parquet('{parquet}')")
            return self.con.sql(f'SELECT * FROM "{table}"')
        return self.con.sql(f'SELECT * FROM "{ref}"')


class Ripple:
    # TODO: runtime wrapper around a registered ripple function — name, func, parents list
    pass


class Trickle:
    # TODO: deferred — incremental/stateful Ripple variant with watermarks and merge semantics
    pass
