"""The shared half of the egress drivers that write into an ATTACHed catalog: Postgres and DuckLake.

Both write plain DuckDB SQL against a database DuckDB attaches (the ``postgres`` and ``ducklake``
extensions), so everything but the ``ATTACH`` itself is common: the in-destination watermark, the
transactional apply, and creating or extending the destination table from the relation's schema. A
subclass supplies :meth:`AttachedCatalogDriver._attach`.

**Apply = delete-then-insert in one transaction.** The changelog window is already consolidated per key,
so deleting the changed and removed keys and re-inserting the present rows is the net effect of an upsert,
and portable (no reliance on an extension's upsert push-down). The **watermark** lives in the destination
and is written in the same transaction, so delivery is exactly-once across Catchment crashes: a crash
mid-apply rolls back, and on restart the worker re-reads the same window and re-applies it.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from .destination import Destination

WATERMARK = "_duckstring_egress"


def qi(ident: str) -> str:
    """Quote a SQL identifier (doubling embedded quotes)."""
    return '"' + ident.replace('"', '""') + '"'


def qs(value: str) -> str:
    """A single-quoted SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


class AttachedCatalogDriver:
    def __init__(self, dest: Destination):
        self.dest = dest
        self._prefix: str | None = None  # the schema-qualified prefix once attached (e.g. "alias"."public")
        self._in_batch = False

    # ─── the seam ────────────────────────────────────────────────────────────

    def _attach(self, con) -> str:
        """ATTACH the destination (once) and return the schema-qualified prefix of its tables."""
        raise NotImplementedError

    def _watermark_table(self, prefix: str) -> str:
        """Where the watermark lives: beside the delivered tables, unless a driver keeps it elsewhere."""
        return f"{prefix}.{qi(WATERMARK)}"

    def _alter_type(self, current: str, incoming: str) -> str | None:
        """The type to change an existing column to when the source's type changed, or None to leave it. Only
        a driver whose catalog keeps DuckDB's types exactly can tell a real change from a type mapping."""
        return None

    # ─── the driver protocol ─────────────────────────────────────────────────

    def ensure(self, *, table: str, schema: dict | None, pk: list[str] | None) -> None:
        pass  # the table is created lazily from the relation's schema on first write (see _ensure_table)

    def test_connection(self, con) -> None:
        """Probe connectivity and credentials: ``ATTACH`` connects eagerly (a bad host or credential fails
        here, sanitised by the subclass), then a trivial query confirms the link. Nothing is created."""
        self._attach(con)
        con.execute("SELECT 1").fetchall()

    def watermark(self, con, table: str) -> datetime | None:
        """The freshness the destination has fully applied for ``table``: the worker's ``previous_f``."""
        self._ensure_attached(con)
        rows = con.execute(
            f"SELECT f FROM {self._watermark_table(self._prefix)} WHERE table_name = ?", [table]
        ).fetchall()
        return datetime.fromisoformat(rows[0][0]) if rows and rows[0][0] else None

    def write_full(self, con, relation, *, table: str, pk: list[str] | None, f: datetime) -> None:
        relation = _user_columns(relation)  # a full read carries _duckstring_f; a delta's rows don't
        prefix = self._ensure_attached(con)
        tgt = self._ensure_table(con, prefix, relation, table, pk)
        cols = ", ".join(qi(c) for c in relation.columns)
        con.register("__ds_full", relation)
        try:
            with self._transaction(con):
                con.execute(f"DELETE FROM {tgt}")  # within the transaction: readers see old or new, never partial
                con.execute(f"INSERT INTO {tgt} ({cols}) SELECT {cols} FROM __ds_full")
                self._set_watermark(con, prefix, table, f)
        finally:
            con.unregister("__ds_full")

    def apply_delta(self, con, delta, *, table: str, pk: list[str] | None, f: datetime) -> None:
        if not pk and self.capabilities().requires_pk:
            raise ValueError(
                f"egress to a transactional destination needs a primary key — table {table!r} has none "
                "(put a merge Trickle, .merge(pk=…), before this Spout)"
            )
        prefix = self._ensure_attached(con)
        tgt = self._ensure_table(con, prefix, delta.upserts, table, pk)
        cols = ", ".join(qi(c) for c in delta.upserts.columns)
        con.register("__ds_up", delta.upserts)
        con.register("__ds_del", delta.deletes)
        try:
            with self._transaction(con):
                if pk:
                    pk_cols = ", ".join(qi(c) for c in pk)
                    # Delete the prior image of every changed key (upserts) and every removed key (deletes)…
                    con.execute(
                        f"DELETE FROM {tgt} WHERE ({pk_cols}) IN "
                        f"(SELECT {pk_cols} FROM __ds_up UNION SELECT {pk_cols} FROM __ds_del)"
                    )
                elif delta.deletes.limit(1).fetchall():
                    # Without a key a removed row can't be found; an append Trickle never retracts.
                    raise ValueError(
                        f"table {table!r} retracted rows but has no primary key to find them by "
                        "(put a merge Trickle, .merge(pk=…), before this Spout)"
                    )
                # …then insert the present rows (deletes carry no positive row, so they stay gone).
                con.execute(f"INSERT INTO {tgt} ({cols}) SELECT {cols} FROM __ds_up")
                self._set_watermark(con, prefix, table, f)  # exactly-once: committed with the data
        finally:
            con.unregister("__ds_up")
            con.unregister("__ds_del")

    # ─── helpers ─────────────────────────────────────────────────────────────

    def _ensure_attached(self, con) -> str:
        """Attach, and ensure the schema and the watermark table exist. Returns the schema-qualified prefix."""
        prefix = self._attach(con)
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {prefix}")
        key = " PRIMARY KEY" if self._declares_pk() else ""
        con.execute(f"CREATE TABLE IF NOT EXISTS {self._watermark_table(prefix)} (table_name VARCHAR{key}, f VARCHAR)")
        return prefix

    @contextmanager
    def _transaction(self, con):
        """One transaction, or none of our own inside a batch (the batch commits everything together)."""
        if self._in_batch:
            yield
            return
        con.execute("BEGIN TRANSACTION")
        try:
            yield
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    def _ensure_table(self, con, prefix: str, relation, table: str, pk: list[str] | None) -> str:
        """Create the destination table from the relation's schema if absent, and add any column the source
        has gained since (the version contract only lets additive changes through within a major). DuckDB
        type names in the DDL; the extension maps them. Returns the quoted target."""
        tgt = f"{prefix}.{qi(table)}"
        incoming = dict(zip(relation.columns, (str(t) for t in relation.types), strict=True))
        coldefs = ", ".join(f"{qi(name)} {dtype}" for name, dtype in incoming.items())
        pkdef = f", PRIMARY KEY ({', '.join(qi(c) for c in pk)})" if pk and self._declares_pk() else ""
        con.execute(f"CREATE TABLE IF NOT EXISTS {tgt} ({coldefs}{pkdef})")
        current = {row[0]: str(row[1]) for row in con.execute(f"DESCRIBE {tgt}").fetchall()}
        for name, dtype in incoming.items():
            if name not in current:
                con.execute(f"ALTER TABLE {tgt} ADD COLUMN {qi(name)} {dtype}")
            elif current[name] != dtype and (to := self._alter_type(current[name], dtype)):
                con.execute(f"ALTER TABLE {tgt} ALTER {qi(name)} TYPE {to}")
        return tgt

    def _declares_pk(self) -> bool:
        """Whether the catalog takes PRIMARY KEY constraints (Postgres does; DuckLake has none)."""
        return True

    def _set_watermark(self, con, prefix: str, table: str, f: datetime) -> None:
        wm = self._watermark_table(prefix)
        con.execute(f"DELETE FROM {wm} WHERE table_name = ?", [table])
        con.execute(f"INSERT INTO {wm} (table_name, f) VALUES (?, ?)", [table, f.isoformat()])


def _user_columns(relation):
    """``relation`` without Duckstring's system columns, so every delivery writes the same columns."""
    from ..trickle.context import SYSTEM_PREFIX

    keep = [c for c in relation.columns if not c.startswith(SYSTEM_PREFIX)]
    return relation if len(keep) == len(relation.columns) else relation.project(", ".join(qi(c) for c in keep))
