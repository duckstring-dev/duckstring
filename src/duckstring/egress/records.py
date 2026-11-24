"""Spouts into Records (plans/records-and-media.md): a Pond's output delivered into the Catchment's own Records,
where every App reads it with an ordinary PostgreSQL driver (fast lookups, concurrent reads).

The destination is ``records:``, which the Catchment expands when the Spout is added to ``records:{pond}_v{major}``:
a schema of that name (the catalog's naming for the Pond line), owned by the ``duckstring_spouts`` role and readable
by every App line. The delivery is the Postgres driver's (exactly-once, a merge Trickle applied by key), into the
Catchment's PostgreSQL, which it starts if need be.
"""

from __future__ import annotations

import re

from .postgres import PostgresEgressDriver

# The Catchment's Records (catchment/records.py), set in create_app: drivers are made by scheme, deep in the worker.
_records = None


def set_records(records) -> None:
    global _records
    _records = records


SCHEMA = re.compile(r"[a-z_][a-z0-9_]*_v[0-9]+")


def schema_of(destination: str) -> str:
    schema = destination.strip()[len("records:"):]
    if not SCHEMA.fullmatch(schema):
        raise ValueError(f"a Records Spout delivers into {{pond}}_v{{major}}, not {schema!r}")
    return schema


class RecordsEgressDriver(PostgresEgressDriver):
    SCHEMES = ("records",)

    def _schema(self) -> str:
        return schema_of(self.dest.raw)

    def _conn_string(self) -> str:
        if _records is None:
            raise RuntimeError("Records aren't available in this process")
        return _records.spout_url(self._schema())
