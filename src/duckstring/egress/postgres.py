"""Postgres egress driver — the flagship incremental (CDC) sink (see plans/egress.md "Postgres egress").

A **merge Trickle's changelog is already a CDC stream**, so a modeled table syncs *incrementally* into an
app's transactional database — a few changed rows per run, not a full reload. ``capabilities = {delta:
True, delete: True, transactional: True}``.

Transport is the **DuckDB ``postgres`` extension** (no SQLAlchemy/psycopg): ``ATTACH`` the database, then
write ordinary DuckDB SQL against the attached tables — the extension maps DuckDB types and pushes the
writes to Postgres. Because all the SQL is plain DuckDB, the apply/upsert/delete/watermark logic is exactly
exercisable against a DuckDB-attached database in tests; the only Postgres-specific surface is the ATTACH.

The apply, the watermark (`_duckstring_egress(table_name, f)`, written in the same transaction, so delivery
is exactly-once) and table creation are shared with DuckLake in :mod:`.attached`; this module is the ATTACH.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit, urlunsplit

from . import credentials
from .attached import AttachedCatalogDriver, qi, qs
from .base import Capabilities

_ALIAS = "duckstring_egress_pg"  # the ATTACH alias on the worker's connection


class PostgresEgressDriver(AttachedCatalogDriver):
    SCHEMES = ("postgres", "postgresql")

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_delta=True, supports_delete=True, transactional=True)

    def _schema(self) -> str:
        q = parse_qs(urlsplit(credentials.resolve(self.dest.raw)).query)  # the URI may be one ${env:} reference
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
                con.execute(f"ATTACH {qs(self._conn_string())} AS {qi(_ALIAS)} (TYPE postgres)")
            except Exception:
                # The connection string carries the password — never let an error echo it.
                raise RuntimeError("failed to connect to the Postgres destination (check the URI / credentials)") from None
            self._prefix = f"{qi(_ALIAS)}.{qi(self._schema())}"
        return self._prefix
