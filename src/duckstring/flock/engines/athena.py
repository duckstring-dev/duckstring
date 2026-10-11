"""The Athena Flock engine: a job translated to Trino SQL and run on Amazon Athena (plans/flock-coverage.md).

Athena isn't DuckDB, so a job is translated (:mod:`duckstring.flock.translate`) and only constructs measured
to agree are sent. Every builder shape is eligible (every join, filters, computed columns, ``.sql()``
queries); what limits Athena is its expression list below, which grows only from measurement against a real
Athena (``tests/test_flock_athena_conformance.py``). Aggregates and division stay local until measured.

Sources are staged as Parquet under the scratch prefix and registered as temporary Glue external tables; the
translated job runs as an ``UNLOAD`` to Parquet, which the Duck reads back. Credentials come from the Duck's
IAM role (the boto3 chain).

Config (``workgroup``, ``database`` and ``scratch`` are required to enable):

- ``DUCKSTRING_FLOCK_ATHENA_WORKGROUP`` — the workgroup queries run in.
- ``DUCKSTRING_FLOCK_ATHENA_DATABASE``  — the Glue database for temporary external tables.
- ``DUCKSTRING_FLOCK_ATHENA_SCRATCH``   — s3:// prefix for staged sources + UNLOAD results.
- ``DUCKSTRING_FLOCK_ATHENA_REGION``    — AWS region (else the boto3 default chain).

Requires ``boto3`` and ``sqlglot`` (``pip install 'duckstring[aws]'``) and DuckDB httpfs.
"""

from __future__ import annotations

import logging
import time

from .. import translate
from .sql_engine import SqlEngine

log = logging.getLogger("duckstring.flock.athena")

# DuckDB type name → Athena/Glue DDL type (the staged Parquet surface; see sql_engine.stage_select).
_TYPES = {
    "BIGINT": "bigint", "INTEGER": "int", "SMALLINT": "smallint", "TINYINT": "tinyint",
    "DOUBLE": "double", "FLOAT": "float", "BOOLEAN": "boolean",
    "VARCHAR": "string", "DATE": "date", "BLOB": "binary", "TIMESTAMP": "timestamp",
}

# ── What Athena (Trino) is trusted with ─────────────────────────────────────────
#
# Equivalence is a property of the construct AND the engine running it, so the list lives here. Each entry
# was measured against real Athena on 2026-08-03 (tests/test_flock_athena_conformance.py), comparing
# POST-conform values, i.e. what would actually ship:
#   agreed  : decimal * and +, round() incl. the half-way and 2-dp cases, abs, floor/ceil, coalesce, null
#             comparison.
#   diverged: 7 / 2 → DuckDB 3.5, Athena 3 (post-conform 3.0: the right type carrying the WRONG value);
#             CAST(2.5 AS INTEGER) on a DOUBLE → DuckDB 2, Athena 3.
#   agreed but still excluded, pending more than one sample each: CAST(2.9 AS INTEGER), upper(trim(…)),
#             string || NULL. One passing case is not a proof about a whole function family.
# sqlglot's Trino translation of `/` (a DOUBLE division with NULLIF on the divisor) fixes the integer
# division but returns NULL where DuckDB returns an infinity, so division waits for a rewrite like StarRocks'
# and a measurement. Aggregates are unmeasured on Athena and keep an .aggregate() local here.
#
# The list grows from evidence: the conformance test runs each candidate's translation on both engines.
ALLOWED = frozenset({"Add", "Sub", "Mul", "Round", "Abs", "Floor", "Ceil", "Nullif", "Least", "Greatest"})
PROFILE = translate.Profile(dialect="athena", allowed=ALLOWED, cast_to=frozenset())


class AthenaEngine(SqlEngine):
    """A :class:`duckstring.flock.FlockEngine` on Athena."""

    PROFILE = PROFILE
    SECRETS: tuple[str, ...] = ()  # credentials come from the Duck's IAM role, not a secret

    def __init__(self, env, name: str | None = None):
        super().__init__(env, name or "athena")
        self.workgroup = self._setting("WORKGROUP")
        self.database = self._setting("DATABASE")
        self.region = self._setting("REGION")
        self._tables: dict[str, list[str]] = {}

    def _setting(self, key: str) -> str | None:
        return self.env.get(f"DUCKSTRING_FLOCK_ATHENA_{key}") or None

    def enabled(self) -> bool:
        return bool(self.workgroup and self.database and self.scratch)

    # ── SqlEngine ─────────────────────────────────────────────────────────────

    def _session(self):
        import boto3

        return boto3.Session(region_name=self.region)

    def prepare(self, con, storage) -> None:
        _duckdb_s3_secret(con, self._session())

    def read_of(self, uri: str, columns: dict[str, str], tag: str, i: int) -> str:
        table = f"dsjob_{tag}_src{i}"
        cols = ", ".join(f"`{c}` {_athena_type(t)}" for c, t in columns.items())
        self._query(f"CREATE EXTERNAL TABLE {self.database}.{table} ({cols}) STORED AS PARQUET LOCATION '{uri}/'")
        self._tables.setdefault(tag, []).append(table)
        return f'"{self.database}"."{table}"'

    def execute(self, sql: str, out_uri: str, tag: str) -> dict:
        qid, stats = self._query(f"UNLOAD ({sql}) TO '{out_uri}' WITH (format = 'PARQUET')", token=f"ds-{tag}")
        return {"query": qid, "scanned_mb": round(stats.get("DataScannedInBytes", 0) / 1e6, 1)}

    def cleanup(self, tag: str) -> None:
        for table in self._tables.pop(tag, []):  # best effort; the scratch lifecycle is the backstop
            try:
                self._query(f"DROP TABLE IF EXISTS {self.database}.{table}")
            except Exception:  # noqa: BLE001
                pass

    def _query(self, sql: str, token: str | None = None):
        """Run one Athena statement to completion. Returns ``(query_id, statistics)``."""
        athena = self._session().client("athena")
        kwargs = {
            "QueryString": sql,
            "WorkGroup": self.workgroup,
            "QueryExecutionContext": {"Database": self.database},
        }
        if token:
            kwargs["ClientRequestToken"] = token.ljust(32, "x")  # Athena wants ≥32 chars
        qid = athena.start_query_execution(**kwargs)["QueryExecutionId"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            ex = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = ex["Status"]["State"]
            if state == "SUCCEEDED":
                return qid, ex.get("Statistics", {})
            if state in ("FAILED", "CANCELLED"):
                raise RuntimeError(f"athena query {state}: {ex['Status'].get('StateChangeReason', '')[:300]}")
            time.sleep(0.5)
        raise TimeoutError(f"athena query {qid} did not finish in 300s")


# ─── helpers ─────────────────────────────────────────────────────────────────────


def _athena_type(duck_type: str) -> str:
    t = duck_type.upper()
    if t.startswith("DECIMAL"):
        return t.lower()
    if t not in _TYPES:
        raise ValueError(f"no Athena mapping for DuckDB type {duck_type}")
    return _TYPES[t]


def _duckdb_s3_secret(con, session) -> None:
    """Load httpfs + feed the boto3 credential chain's keys to DuckDB for s3:// I/O."""
    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials in the boto3 chain")
    frozen = creds.get_frozen_credentials()
    con.execute("INSTALL httpfs; LOAD httpfs")
    region = session.region_name or "us-east-1"
    token = f", SESSION_TOKEN '{frozen.token}'" if frozen.token else ""
    con.execute(
        "CREATE OR REPLACE SECRET ds_flock_athena ("
        f"TYPE S3, KEY_ID '{frozen.access_key}', SECRET '{frozen.secret_key}'{token}, "
        f"REGION '{region}')"
    )
