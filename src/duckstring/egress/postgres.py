"""Postgres egress driver — the flagship incremental (CDC) sink (see plans/egress.md "Postgres egress").

A **merge Trickle's changelog is already a CDC stream**, so a modeled table syncs *incrementally* into an
app's transactional database — a few changed rows per run, not a full reload. ``capabilities = {delta:
True, delete: True, transactional: True}``.

Transport is the **DuckDB ``postgres`` extension** (no SQLAlchemy/psycopg): ``ATTACH`` the database, then
write ordinary DuckDB SQL against the attached tables — the extension maps DuckDB types and pushes the
writes to Postgres. Because all the SQL is plain DuckDB, the apply/upsert/delete/watermark logic is exactly
exercisable against a DuckDB-attached database in tests; the only Postgres-specific surface is the ATTACH.

**Apply = delete-then-insert in one transaction** (not ``INSERT … ON CONFLICT``): the changelog window is
already consolidated per key (latest op wins), so deleting the changed/removed keys and re-inserting the
present rows is the same net effect, and it's portable (no reliance on the extension's upsert push-down).
The **watermark** lives *in the destination* (`_duckstring_egress(table_name, f)`) and is set **inside the
same transaction** — so egress is **exactly-once** to Postgres across Catchment crashes: a crash mid-apply
rolls back; on restart the worker re-reads the same window (which is now empty) and re-applies a no-op.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlsplit, urlunsplit

from . import credentials
from .base import Capabilities
from .destination import Destination

_WATERMARK = "_duckstring_egress"
_ALIAS = "duckstring_egress_pg"  # the ATTACH alias on the worker's connection


def _qi(ident: str) -> str:
    """Quote a SQL identifier (doubling embedded quotes)."""
    return '"' + ident.replace('"', '""') + '"'


def _qs(value: str) -> str:
    """A single-quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


class PostgresEgressDriver:
    SCHEMES = ("postgres", "postgresql")

    def __init__(self, dest: Destination):
        self.dest = dest
        self._prefix: str | None = None  # the schema-qualified prefix once attached (e.g. "alias"."public")

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_delta=True, supports_delete=True, transactional=True)

    # ─── connection / attach ────────────────────────────────────────────────

    def _schema(self) -> str:
        q = parse_qs(urlsplit(self.dest.raw).query)
        return q.get("schema", ["public"])[0]

    def _conn_string(self) -> str:
        """The libpq connection URI for ``ATTACH`` — credentials ``${env}`` resolved, our own ``schema``
        query param removed (it is not a libpq parameter)."""
        parts = urlsplit(credentials.resolve(self.dest.raw))
        q = [(k, v) for k, v in parse_qs(parts.query).items() if k != "schema"]
        query = "&".join(f"{k}={v[0]}" for k, v in q)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))

    def _attach(self, con) -> str:
        """ATTACH the Postgres database (once) and return the schema-qualified prefix. The single
        Postgres-specific seam — overridden in tests to attach a DuckDB database instead."""
        if self._prefix is None:
            try:
                con.execute(f"ATTACH {_qs(self._conn_string())} AS {_qi(_ALIAS)} (TYPE postgres)")
            except Exception:
                # The connection string carries the password — never let an error echo it.
                raise RuntimeError("failed to connect to the Postgres destination (check the URI / credentials)") from None
            self._prefix = f"{_qi(_ALIAS)}.{_qi(self._schema())}"
        return self._prefix

    def _ensure_attached(self, con) -> str:
        """Attach + ensure the schema and watermark table exist. Returns the schema-qualified prefix."""
        prefix = self._attach(con)
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {prefix}")
        con.execute(f"CREATE TABLE IF NOT EXISTS {prefix}.{_qi(_WATERMARK)} "
                    "(table_name VARCHAR PRIMARY KEY, f VARCHAR)")
        return prefix

    # ─── seam ────────────────────────────────────────────────────────────────

    def ensure(self, *, table: str, schema: dict | None, pk: list[str] | None) -> None:
        pass  # the table is created lazily from the relation's schema on first write (see _ensure_table)

    def test_connection(self, con) -> None:
        """Probe connectivity + credentials: ``ATTACH`` eagerly connects (a bad host/credential fails
        here, sanitised so the password never echoes), then a trivial query confirms the link. No schema
        or table is created — this is lighter than ``ensure``."""
        self._attach(con)
        con.execute("SELECT 1").fetchall()

    def watermark(self, con, table: str) -> datetime | None:
        """The freshness the destination has fully applied for ``table`` — the worker's ``previous_f``."""
        prefix = self._ensure_attached(con)
        row = con.execute(
            f"SELECT f FROM {prefix}.{_qi(_WATERMARK)} WHERE table_name = ?", [table]
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def write_full(self, con, relation, *, table: str, pk: list[str] | None, f: datetime) -> None:
        prefix = self._ensure_attached(con)
        tgt = self._ensure_table(con, prefix, relation, table, pk)
        cols = ", ".join(_qi(c) for c in relation.columns)
        con.register("__ds_full", relation)
        try:
            con.execute("BEGIN TRANSACTION")
            con.execute(f"DELETE FROM {tgt}")  # within the txn → readers see old-or-new, never partial
            con.execute(f"INSERT INTO {tgt} ({cols}) SELECT {cols} FROM __ds_full")
            self._set_watermark(con, prefix, table, f)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.unregister("__ds_full")

    def apply_delta(self, con, delta, *, table: str, pk: list[str] | None, f: datetime) -> None:
        if not pk:
            raise ValueError(
                f"egress to a transactional destination needs a primary key — table {table!r} has none "
                "(put a merge Trickle, .merge(pk=…), before this Spout)"
            )
        prefix = self._ensure_attached(con)
        tgt = self._ensure_table(con, prefix, delta.upserts, table, pk)
        cols = ", ".join(_qi(c) for c in delta.upserts.columns)
        pk_cols = ", ".join(_qi(c) for c in pk)
        con.register("__ds_up", delta.upserts)
        con.register("__ds_del", delta.deletes)
        try:
            con.execute("BEGIN TRANSACTION")
            # Delete the prior image of every changed key (upserts) and every removed key (deletes)…
            con.execute(
                f"DELETE FROM {tgt} WHERE ({pk_cols}) IN "
                f"(SELECT {pk_cols} FROM __ds_up UNION SELECT {pk_cols} FROM __ds_del)"
            )
            # …then re-insert the present rows. (deletes carry no positive row, so they stay gone.)
            con.execute(f"INSERT INTO {tgt} ({cols}) SELECT {cols} FROM __ds_up")
            self._set_watermark(con, prefix, table, f)  # exactly-once: committed with the data
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.unregister("__ds_up")
            con.unregister("__ds_del")

    # ─── helpers ───────────────────────────────────────────────────────────────

    def _ensure_table(self, con, prefix: str, relation, table: str, pk: list[str] | None) -> str:
        """Create the destination table from the relation's schema if absent (idempotent). DuckDB type
        names in the DDL — the postgres extension maps them to Postgres types. Returns the quoted target."""
        tgt = f"{prefix}.{_qi(table)}"
        coldefs = ", ".join(
            f"{_qi(name)} {dtype}" for name, dtype in zip(relation.columns, relation.types, strict=True)
        )
        pkdef = f", PRIMARY KEY ({', '.join(_qi(c) for c in pk)})" if pk else ""
        con.execute(f"CREATE TABLE IF NOT EXISTS {tgt} ({coldefs}{pkdef})")
        return tgt

    def _set_watermark(self, con, prefix: str, table: str, f: datetime) -> None:
        wm = f"{prefix}.{_qi(_WATERMARK)}"
        con.execute(f"DELETE FROM {wm} WHERE table_name = ?", [table])
        con.execute(f"INSERT INTO {wm} (table_name, f) VALUES (?, ?)", [table, f.isoformat()])
