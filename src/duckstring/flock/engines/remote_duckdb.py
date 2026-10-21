"""Flock engines that run DuckDB somewhere else (plans/flock-motherduck.md, plans/flock-coverage.md).

The offered job is one self-contained DuckDB query (:meth:`trickle.ComprehensiveJob.sql`), and the remote
engine is DuckDB too, so the query runs **unchanged**: every builder shape, every expression, ``.sql()``
and an ``.aggregate()``'s accumulator rebuild are eligible, and results match by construction. ``conform``
stays as the guard against a DuckDB version difference between the two ends.

How each source reaches the remote side:

- **Read in place**: a Source table whose published copy on the object-store data root is current is read
  there by the remote engine (``Pond.flock_source_select``), so nothing is sent.
- **Uploaded**: anything else (an own table, a chained intermediate, a Source only published on this
  machine, a table a ``.sql()`` query names) is streamed from the Duck as Arrow into a scratch table on the
  remote side, cast to its local DuckDB types so the remote query sees exactly what the local one would.
  Uploaded tables are dropped when the job ends.

The result streams back as Arrow into a temp table on the Duck's connection.

Subclasses supply the connection (:meth:`RemoteDuckDBEngine.connect`) and where uploads go
(:meth:`RemoteDuckDBEngine.scratch`).
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from ...trickle_io import first_row

log = logging.getLogger("duckstring.flock.remote")

_BATCH_ROWS = 1_000_000


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _reader(result):
    """A streaming Arrow reader over a relation or query result (``to_arrow_reader`` where this DuckDB has
    it, else the older ``fetch_record_batch``)."""
    to_reader = getattr(result, "to_arrow_reader", None)
    return to_reader(_BATCH_ROWS) if to_reader is not None else result.fetch_record_batch(_BATCH_ROWS)


def _walk(node, visit) -> None:
    if isinstance(node, dict):
        visit(node)
        for v in node.values():
            _walk(v, visit)
    elif isinstance(node, list):
        for v in node:
            _walk(v, visit)


def query_tables(con, query: str) -> list[tuple[str, str, str]]:
    """The base tables ``query`` reads, as ``(catalog, schema, table)``, leaving out its own CTEs. Parsed by
    DuckDB (``json_serialize_sql``); raises ``ValueError`` on SQL DuckDB can't parse."""
    tree = json.loads(first_row(con.execute("SELECT json_serialize_sql(?)", [query]))[0])
    if tree.get("error"):
        raise ValueError(tree.get("error_message", "unparseable SQL"))
    ctes: set[str] = set()
    refs: list[tuple[str, str, str]] = []

    def visit(n: dict) -> None:
        cte_map = n.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map", []):
                if isinstance(entry, dict) and entry.get("key"):
                    ctes.add(entry["key"].lower())
        if n.get("type") == "BASE_TABLE":
            refs.append((n.get("catalog_name") or "", n.get("schema_name") or "", n.get("table_name") or ""))

    _walk(tree, visit)
    return [r for r in refs if r[0] or r[1] or r[2].lower() not in ctes]


def extra_tables(job) -> list[str]:
    """The tables a job's ``.sql()`` queries read besides the results they are given (their aliases): each
    must be sent to the remote side. Raises ``ValueError`` for a reference the job can't send (one qualified
    by a schema or catalog)."""
    aliases = set()
    b = job.builder
    while b._sql_of is not None:
        aliases.add(b._sql_of[2].lower())
        b = b._sql_of[0]
    out: list[str] = []
    for query in job.queries():
        for catalog, schema, table in query_tables(job.ctx.con, query):
            if catalog or (schema and schema.lower() not in ("main", "temp")):
                raise ValueError(f"the .sql() query reads {'.'.join(p for p in (catalog, schema, table) if p)}, "
                                 "which can't be sent; read it by its plain name")
            if table.lower() not in aliases and table not in out:
                out.append(table)
    return out


class RemoteDuckDBEngine:
    """The shared remote-DuckDB engine. A :class:`duckstring.flock.FlockEngine`."""

    SECRETS: tuple[str, ...] = ()
    # Whether the remote side shares this machine's filesystem, so a Source published locally can be read in
    # place rather than uploaded. True only for a stand-in running on the Duck itself (the tests).
    reads_local_paths = False

    def __init__(self, env, name: str | None = None):
        self.env = env
        self.name = name or "remote-duckdb"
        self.last_error: str | None = None

    # ── what a target supplies ────────────────────────────────────────────────

    def connect(self):
        """A DuckDB connection whose queries run on the remote engine."""
        raise NotImplementedError

    def scratch(self, remote) -> str:
        """The qualified schema uploaded tables are created in (made ready on ``remote``)."""
        return "main"

    # ── FlockEngine protocol ──────────────────────────────────────────────────

    def enabled(self) -> bool:
        return True

    def eligible(self, job) -> str | None:
        if job.kind not in ("rows", "aggregate"):
            return f"a {job.kind!r} job isn't something this engine runs"
        try:
            extra_tables(job)
        except ValueError as exc:
            return str(exc)
        return None

    def estimate_rows(self, job) -> int:
        from .. import leaf_rows

        return leaf_rows(job)

    def dispatch(self, job):
        """Run the job remotely; return a relation over the result on the Duck's connection, or ``None`` on
        any failure (the caller computes locally)."""
        try:
            result = self._run(job)
            self.last_error = None
            return result
        except Exception as exc:  # noqa: BLE001 (the fallback contract)
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("flock/%s: dispatch failed (%s); running local", self.name, self.last_error)
            return None

    # ── execution ─────────────────────────────────────────────────────────────

    def _run(self, job):
        ctx = job.ctx
        con = ctx.con
        tag = uuid.uuid4().hex[:12]
        started = time.monotonic()
        remote = self.connect()
        uploaded: list[str] = []
        try:
            schema = self.scratch(remote)
            select = getattr(ctx, "flock_source_select", None)
            reads: dict[int, str] = {}
            in_place = 0
            for i, leaf in enumerate(job.leaves()):
                cols = job.columns(leaf)
                sql = select(leaf.ref, cols, object_store_only=not self.reads_local_paths) if select else None
                if sql is None:
                    table = f"{schema}.{_q(f'dsflock_{tag}_src{i}')}"
                    self._upload(remote, ctx.read_table(leaf.ref), table)
                    uploaded.append(table)
                    sql = f"SELECT {', '.join(_q(c) for c in cols)} FROM {table}"
                else:
                    in_place += 1
                reads[id(leaf)] = sql
            body = job.sql(lambda leaf: reads[id(leaf)])
            extras = extra_tables(job)
            if extras:
                ctes = []
                for j, name in enumerate(extras):
                    table = f"{schema}.{_q(f'dsflock_{tag}_tbl{j}')}"
                    self._upload(remote, con.table(name), table)
                    uploaded.append(table)
                    ctes.append(f"{_q(name)} AS NOT MATERIALIZED (SELECT * FROM {table})")
                body = f"WITH {', '.join(ctes)} SELECT * FROM ({body}) AS {_q('_duckstring_job')}"
            result = self._fetch(remote, body, con, f"_ds_flock_{tag}")
            log.warning("flock: dispatched via %s: %d source(s) read in place, %d uploaded, %d ms",
                        self.name, in_place, len(uploaded), int((time.monotonic() - started) * 1000))
            return result
        finally:
            for table in uploaded:  # best effort; a scratch table left behind holds no live state
                try:
                    remote.execute(f"DROP TABLE IF EXISTS {table}")
                except Exception:  # noqa: BLE001
                    pass
            try:
                remote.close()
            except Exception:  # noqa: BLE001
                pass

    def _upload(self, remote, rel, table: str) -> None:
        """Stream a local relation into ``table`` on the remote side, cast back to its local DuckDB types
        (Arrow carries some types as others, e.g. HUGEINT as DECIMAL(38,0))."""
        view = f"_ds_up_{uuid.uuid4().hex[:8]}"
        remote.register(view, _reader(rel))
        try:
            casts = ", ".join(f"CAST({_q(c)} AS {t}) AS {_q(c)}" for c, t in zip(rel.columns, rel.types, strict=True))
            remote.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT {casts} FROM {_q(view)}")
        finally:
            remote.unregister(view)

    @staticmethod
    def _fetch(remote, sql: str, con, name: str):
        """Run ``sql`` remotely and land the result in a temp table on ``con``."""
        reader = _reader(remote.execute(sql))
        view = f"{name}_in"
        con.register(view, reader)
        try:
            con.execute(f"CREATE TEMP TABLE {_q(name)} AS SELECT * FROM {_q(view)}")
        finally:
            con.unregister(view)
        return con.table(name)
