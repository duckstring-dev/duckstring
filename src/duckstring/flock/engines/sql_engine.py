"""The shared machinery of the Flock's SQL engines (StarRocks, Athena): engines that aren't DuckDB, so a job
is translated (:mod:`duckstring.flock.translate`) and only what the engine was measured to agree on is sent.

A dispatch:

1. **Stage** each source's current state as Parquet under the engine's scratch prefix, with the types every
   engine reads the same way (TIMESTAMPTZ as a UTC wall-clock TIMESTAMP, HUGEINT as DECIMAL(38,0)).
2. **Run** the translated query on the engine, which writes its result as Parquet under the scratch prefix
   (each engine supplies :meth:`SqlEngine.execute`).
3. **Read back** the result on the Duck and restore what the trip changed: column names DuckDB would give
   (sqlglot normalises case), TIMESTAMPTZ from UTC, and an aggregate's freshness stamp, which is a constant
   added here rather than sent.

``conform`` then casts the result to DuckDB's own schema for the job, and :func:`duckstring.flock.verify`
recomputes a sample locally before it is used.
"""

from __future__ import annotations

import logging
import time
import uuid

from .. import translate

log = logging.getLogger("duckstring.flock.sql")

# The DuckDB types a source column may have to be staged (others keep the job local). Engines read each of
# these from Parquet the same way.
_STAGEABLE = ("BOOLEAN", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "VARCHAR",
              "DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE")
_F_COL = "_duckstring_f"


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _location(storage) -> str:
    """A storage location as the engine addresses it: the object-store URI, or a local path."""
    return getattr(storage, "base", None) or str(storage.root)


def stage_select(rel) -> str:
    """A query over ``rel`` with every column in a form all engines read the same way from Parquet."""
    cols = []
    for c, t in zip(rel.columns, (str(t) for t in rel.types), strict=True):
        if t == "TIMESTAMP WITH TIME ZONE":
            cols.append(f"timezone('UTC', {_q(c)}) AS {_q(c)}")
        elif t == "HUGEINT":
            cols.append(f"CAST({_q(c)} AS DECIMAL(38,0)) AS {_q(c)}")
        else:
            cols.append(_q(c))
    return f"SELECT {', '.join(cols)} FROM ({rel.sql_query()})"


def unstageable(job) -> str | None:
    """The reason a source column's type can't be staged, or ``None``."""
    for leaf in job.leaves():
        rel = job.ctx.read_table(leaf.ref)
        for c, t in zip(rel.columns, (str(t) for t in rel.types), strict=True):
            if t not in _STAGEABLE and not t.startswith("DECIMAL"):
                return f"source {leaf.ref} column {c} is {t}, which isn't sent to a SQL engine"
    return None


def accepted(env) -> tuple[str, ...]:
    """The acceptance kinds the run's Pond accepts (``DUCKSTRING_FLOCK_ACCEPT``, comma-separated)."""
    raw = (env.get("DUCKSTRING_FLOCK_ACCEPT") or "").replace(";", ",")
    return tuple(k.strip() for k in raw.split(",") if k.strip() in translate.ACCEPTANCES)


class SqlEngine:
    """Base for SQL engines. A subclass sets :attr:`PROFILE` and implements :meth:`execute`, :meth:`read_of`
    and :meth:`enabled`."""

    PROFILE: translate.Profile
    SECRETS: tuple[str, ...] = ()
    VERIFY = True  # flock._dispatch recomputes a sample of keys locally (duckstring.flock.verify)

    def __init__(self, env, name: str | None = None):
        self.env = env
        self.name = name or type(self).__name__
        self.scratch = (self._setting("SCRATCH") or "").rstrip("/") or None
        self.accept = accepted(env)
        self.last_error: str | None = None
        self._translations: dict[int, translate.Translation] = {}
        self.measuring = False  # skip the profile: the conformance tests measure constructs this way

    def _setting(self, key: str) -> str | None:
        raise NotImplementedError

    # ── what an engine supplies ───────────────────────────────────────────────

    def read_of(self, uri: str, columns: dict[str, str], tag: str, i: int) -> str:
        """The engine's own read of source ``i``, staged as Parquet in the directory ``uri`` with ``columns``
        (name → staged DuckDB type), as a table expression."""
        raise NotImplementedError

    def prepare(self, con, storage) -> None:
        """Let DuckDB on the Duck write the scratch prefix."""
        storage.duckdb_setup(con)

    def execute(self, sql: str, out_uri: str, tag: str) -> dict:
        """Run ``sql`` and write its result as Parquet files under ``out_uri``; return statistics."""
        raise NotImplementedError

    def cleanup(self, tag: str) -> None:
        """Remove what a dispatch left on the engine (best effort)."""

    # ── FlockEngine protocol ──────────────────────────────────────────────────

    def enabled(self) -> bool:
        return bool(self.scratch)

    def eligible(self, job) -> str | None:
        reason = unstageable(job)
        if reason is not None:
            return reason
        t = self._translate(job)
        return t.reason

    def estimate_rows(self, job) -> int:
        from .. import leaf_rows

        return leaf_rows(job)

    def dispatch(self, job):
        try:
            result = self._run(job)
            self.last_error = None
            return result
        except Exception as exc:  # noqa: BLE001 (the fallback contract)
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("flock/%s: dispatch failed (%s); running local", self.name, self.last_error)
            return None

    # ── execution ─────────────────────────────────────────────────────────────

    def _translate(self, job) -> translate.Translation:
        t = self._translations.get(id(job))
        if t is None:
            drop = (_F_COL,) if job.kind == "aggregate" else ()
            t = translate.translate(job, self.PROFILE, self.accept, drop_columns=drop, check=not self.measuring)
            self._translations[id(job)] = t
        return t

    def _storage(self):
        from ...storage import get_storage

        return get_storage(self.scratch)

    def _run(self, job):
        t = self._translate(job)
        if t.sql is None:
            raise RuntimeError(t.reason)
        con = job.ctx.con
        tag = uuid.uuid4().hex[:12]
        storage = self._storage()
        self.prepare(con, storage)
        started = time.monotonic()
        stage = storage.child("stage", tag)
        try:
            reads = []
            for i, leaf in enumerate(t.leaves):
                stage.mkdir(f"src{i}")  # a no-op on an object store
                staged = con.sql(stage_select(job.ctx.read_table(leaf.ref)))
                con.execute(f"COPY ({staged.sql_query()}) TO '{stage.uri(f'src{i}', 'data.parquet')}' "
                            "(FORMAT PARQUET)")
                columns = dict(zip(staged.columns, (str(t) for t in staged.types), strict=True))
                reads.append(self.read_of(_location(stage.child(f"src{i}")), columns, tag, i))
            sql = translate.substitute(t.sql, lambda i: reads[i])
            stats = self.execute(sql, _location(stage.child("out")) + "/", tag)
            rel = con.read_parquet(stage.glob("*.parquet", "out"))
            log.warning("flock: dispatched via %s: %d source(s) staged, %d ms %s", self.name, len(reads),
                        int((time.monotonic() - started) * 1000), stats or "")
            return self._restore(job, rel, tag)
        finally:
            self.cleanup(tag)

    def _restore(self, job, rel, tag: str):
        """The engine's result with what the trip changed put back: DuckDB's column names (matched without
        case), TIMESTAMPTZ read as UTC, and an aggregate's constant freshness stamp. Lands in a temp table, so
        the staged files can be removed."""
        from ...trickle_io import _ts

        oracle = job.schema()
        by_lower = {c.lower(): c for c in rel.columns}
        types = dict(zip(rel.columns, (str(t) for t in rel.types), strict=True))
        proj = []
        for c, t in oracle.items():
            if c == _F_COL and job.kind == "aggregate":
                proj.append(f"{_ts(job.ctx.f)} AS {_q(c)}")
                continue
            src = by_lower.get(c.lower())
            if src is None:
                continue  # conform rejects the result for the missing column
            expr = _q(src)
            if t == "TIMESTAMP WITH TIME ZONE" and types[src] == "TIMESTAMP":
                expr = f"timezone('UTC', {expr})"
            proj.append(f"{expr} AS {_q(c)}")
        extra = [c for c in rel.columns if c.lower() not in {o.lower() for o in oracle}]
        proj += [_q(c) for c in extra]  # left in, so conform sees (and refuses) a different column set
        name = f"_ds_flock_{tag}"
        job.ctx.con.execute(f"CREATE TEMP TABLE {_q(name)} AS SELECT {', '.join(proj)} FROM ({rel.sql_query()})")
        return job.ctx.con.table(name)
