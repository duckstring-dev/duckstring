"""The Databricks SQL Flock engine (plans/databricks-bigquery.md, part 3): a job translated to Databricks SQL and
run on a SQL warehouse, on any cloud.

Databricks isn't DuckDB, so a job is translated (:mod:`duckstring.flock.translate`) and only constructs whose
translation was measured to agree are sent (the list below, from ``tests/test_flock_databricks_conformance.py``
against a real workspace); the Pond can accept known, bounded differences by name. Every builder shape is
eligible.

Staging needs no external location or storage credential, only a Unity Catalog **Volume** the caller can write:
each source is written as Parquet on the Duck, uploaded to the Volume through the Files API, and read by the
warehouse with ``read_files``. The result comes back through the Statement Execution API as Arrow chunks, which
the Duck writes as Parquet locally and reads. So the engine works from any Duck, inside Databricks or not.

Config (``host``, ``warehouse`` and ``volume`` are required to enable):

- ``DUCKSTRING_FLOCK_DATABRICKS_HOST``: the workspace URL (else ``DATABRICKS_HOST``, which a Databricks App has).
- ``DUCKSTRING_FLOCK_DATABRICKS_WAREHOUSE``: the SQL warehouse id. A serverless warehouse starts in seconds.
- ``DUCKSTRING_FLOCK_DATABRICKS_VOLUME``: a Volume path for staged sources, ``/Volumes/{catalog}/{schema}/{volume}``
  plus an optional directory.
- ``DUCKSTRING_FLOCK_DATABRICKS_TIMEOUT``: seconds a dispatch may run (default 900), including the warehouse
  starting.
- credentials as :mod:`duckstring.databricks_rest` describes: the secrets ``DATABRICKS_TOKEN``, or
  ``DATABRICKS_CLIENT_ID`` and ``DATABRICKS_CLIENT_SECRET``; on Azure, an Entra token.

Requires sqlglot (``pip install 'duckstring[databricks]'``).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ... import databricks_rest as dbx
from .. import translate
from .sql_engine import SqlEngine

# ── What Databricks is trusted with ────────────────────────────────────────────
#
# Each construct below was run on DuckDB and, translated (sqlglot plus the rewrites below), on a Databricks SQL
# warehouse over the same staged data, and compared after conform: the battery and report are
# tests/test_flock_databricks_conformance.py (python tests/test_flock_databricks_conformance.py prints the full
# report). Run it after a sqlglot upgrade.
#
# Measured 2026-11-08 (Azure Databricks, Australia Southeast, a serverless SQL warehouse on Databricks SQL 4.2;
# sqlglot 30.22): these agreed exactly, or (KINDS) within float_order. About 1.2 s per dispatch on a warm warehouse.
#
# Kept out, each for a measured reason:
#   floor, ceil on DOUBLE     Databricks returns a BIGINT (1e300 clamped to 2^63); with a scale, a DECIMAL that
#                             can't hold it (refused by _check).
#   // on DECIMAL             DuckDB keeps the fraction (1.25), Databricks doesn't (the int_only rule).
#   CAST(DOUBLE AS VARCHAR)   formats differ ('1e+300' against '1.0E300'; the translator's rule).
#   CAST(VARCHAR AS …)        DuckDB raises on a malformed string where an engine may not (the translator's rule).
#   date_trunc                Databricks returns a TIMESTAMP (an instant) from a TIMESTAMP_NTZ or a DATE, so the
#                             result depends on the warehouse's and the Duck's time zones.
#   median                    interpolates differently on DECIMAL.
#   strftime                  format codes only partly shared.
ALLOWED = frozenset({
    # arithmetic (Div, CAST to an integer and DayOfWeek are rewritten, see _rewrite)
    "Add", "Sub", "Mul", "Div", "IntDiv", "Mod", "Abs", "Floor", "Ceil", "Round", "Sign", "Greatest", "Least",
    "Nullif", "Pow", "Sqrt", "Ln", "Exp", "Log",
    # strings
    "Upper", "Lower", "Length", "Substring", "Concat", "DPipe", "Trim", "Replace", "Like", "ILike", "StartsWith",
    "Pad", "Reverse", "Left", "StrPosition", "Contains",
    # dates
    "Year", "Month", "Day", "DayOfWeek", "Quarter", "Interval", "DateDiff", "Extract",
    # aggregates
    "Count", "Sum", "Avg", "Min", "Max", "VariancePop", "Variance", "StddevPop", "Stddev", "StddevSamp",
    "CovarPop", "Corr", "RegrSlope", "RegrSxx", "RegrCount", "ArgMin", "ArgMax", "LogicalAnd", "LogicalOr",
    "BitwiseAndAgg", "BitwiseOrAgg", "Filter", "GroupConcat",
    # windows
    "RowNumber", "Rank", "DenseRank", "Lag", "Lead", "FirstValue",
})
# float_order: floating-point results that differ in the last bits. Sum and Avg are exact on integer and DECIMAL
# input (FLOAT_KINDS); the statistics compute in floating point whatever the input, and ln/exp/pow/log aren't
# correctly rounded by every math library.
KINDS = {name: "float_order" for name in (
    "Sum", "Avg", "VariancePop", "Variance", "StddevPop", "Stddev", "StddevSamp", "CovarPop", "Corr", "RegrSlope",
    "RegrSxx", "Log", "Ln", "Exp", "Pow")}
FLOAT_KINDS = frozenset({"Sum", "Avg"})
_FAIL = "duckstring: a division by zero Databricks cannot reproduce"
_STAGE_FILE_BYTES = 1 << 30  # a Files API upload is limited to about 5 GiB


def _type(node) -> str:
    t = getattr(node, "type", None)
    return t.this.name if t is not None else "UNKNOWN"


def _rewrite(tree, exp):
    """DuckDB's semantics spelled out in Databricks SQL where Databricks' own differ:

    - ``a / b`` is DOUBLE division in DuckDB, giving ±inf (or NaN for ``0/0``) for a zero divisor where Databricks
      raises (ANSI mode) or returns NULL. Reproduced for an integer or DECIMAL divisor; a DOUBLE zero divisor may
      be -0.0, which flips the infinity's sign, so a row with one raises and the job runs locally.
    - ``CAST(x AS <integer>)`` rounds in DuckDB (DECIMAL half away from zero, DOUBLE half to even) where
      Databricks truncates: ``round`` (half up) and ``bround`` (half even) first.
    - ``dayofweek`` counts Sunday as 0 in DuckDB and 1 in Databricks.
    """
    import sqlglot

    def parse(sql):
        out = sqlglot.parse_one(sql, read="duckdb")
        for node in out.walk():
            node.set("_duckstring", True)
        return out

    def fix(node):
        if node.args.get("_duckstring"):
            return node
        if isinstance(node, exp.Div):
            a = f"CAST(({node.this.sql('duckdb')}) AS DOUBLE)"
            b = f"CAST(({node.expression.sql('duckdb')}) AS DOUBLE)"
            zero = (f"WHEN {a} > 0 THEN CAST('Infinity' AS DOUBLE) WHEN {a} < 0 THEN CAST('-Infinity' AS DOUBLE) "
                    f"WHEN {a} = 0 THEN CAST('NaN' AS DOUBLE)")
            if _type(node.expression) in ("FLOAT", "DOUBLE", "UNKNOWN"):
                zero = f"WHEN {a} IS NOT NULL THEN CAST(raise_error('{_FAIL}') AS DOUBLE)"
            return parse(f"CASE WHEN {b} = 0 THEN CASE {zero} END ELSE {a} / {b} END")
        if isinstance(node, exp.Cast):
            to = node.to.this.name if isinstance(node.to, exp.DataType) else ""
            src = _type(node.this)
            if to not in translate._INT_TYPES or src in translate._INT_TYPES:
                return node
            x = node.this.sql("duckdb")
            fn = "round" if src.startswith("DECIMAL") or src == "BIGDECIMAL" else "bround"
            return parse(f"CAST({fn}({x}) AS {node.to.sql('duckdb')})")
        if isinstance(node, exp.DayOfWeek):
            return parse(f"(dayofweek({node.this.sql('duckdb')}) - 1)")
        return node

    return tree.transform(fix)


def _check(node, exp) -> str | None:
    """Databricks' refusals beyond construct names."""
    if isinstance(node, (exp.Floor, exp.Ceil)) and _type(node.this) in ("FLOAT", "DOUBLE", "UNKNOWN"):
        return "floor/ceil of a DOUBLE isn't sent: Databricks returns a BIGINT"
    return None


PROFILE = translate.Profile(
    dialect="databricks",
    rewrite=_rewrite,
    allowed=ALLOWED,
    kinds=KINDS,
    float_kinds=FLOAT_KINDS,
    int_only=frozenset({"IntDiv", "Mod"}),
    check=_check,
    cast_to=frozenset({"BIGINT", "INT", "SMALLINT", "TINYINT", "DOUBLE", "DECIMAL", "VARCHAR", "TEXT", "DATE",
                       "TIMESTAMP", "TIMESTAMPNTZ"}),
)

# Databricks result types (the statement manifest's type_name) as DuckDB types, for an empty result.
class DatabricksEngine(SqlEngine):
    """A :class:`duckstring.flock.FlockEngine` on a Databricks SQL warehouse."""

    PROFILE = PROFILE
    SECRETS: tuple[str, ...] = dbx.SECRETS
    STAGE_FILE_BYTES = _STAGE_FILE_BYTES

    def __init__(self, env, name: str | None = None):
        super().__init__(env, name or "databricks")
        self.host = self._setting("HOST") or env.get("DATABRICKS_HOST")
        self.warehouse = self._setting("WAREHOUSE")
        self.volume = (self._setting("VOLUME") or "").rstrip("/") or None
        self.timeout = int(self._setting("TIMEOUT") or 900)
        self._local: str | None = None
        self._client: dbx.Client | None = None
        self.transport = None  # an httpx transport, for tests

    def _setting(self, key: str) -> str | None:
        return self.env.get(f"DUCKSTRING_FLOCK_DATABRICKS_{key}") or None

    def enabled(self) -> bool:
        return bool(self.host and self.warehouse and self.volume)

    def client(self) -> dbx.Client:
        if self._client is None:
            self._client = dbx.Client(self.host, self.env, transport=self.transport)
        return self._client

    # ── SqlEngine ─────────────────────────────────────────────────────────────

    def _storage(self):
        """Staging is local; read_of uploads each source to the Volume."""
        from ...storage import get_storage

        if self._local is None:
            self._local = tempfile.mkdtemp(prefix="duckstring-flock-databricks-")
        return get_storage(self._local)

    def prepare(self, con, storage) -> None:
        pass

    def read_of(self, uri: str, columns: dict[str, str], tag: str, i: int) -> str:
        remote = f"{self.volume}/{tag}/src{i}"
        for path in sorted(Path(uri).glob("*.parquet")):
            self.client().upload(path, f"{remote}/{path.name}")
        return f"read_files({dbx.sql_string(remote)}, format => 'parquet', schemaEvolutionMode => 'none')"

    def execute(self, sql: str, out_uri: str, tag: str) -> dict:
        st = self.client().execute(self.warehouse, sql, arrow=True, timeout_s=self.timeout)
        return self.client().save_result(st, out_uri)

    def cleanup(self, tag: str) -> None:
        try:  # best effort
            self.client().remove_tree(f"{self.volume}/{tag}")
        except Exception:  # noqa: BLE001
            pass
        if self._local:
            shutil.rmtree(os.path.join(self._local, "stage", tag), ignore_errors=True)
