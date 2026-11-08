"""The BigQuery Flock engine (plans/databricks-bigquery.md, part 1): a job translated to GoogleSQL and run on
BigQuery.

BigQuery isn't DuckDB, so a job is translated (:mod:`duckstring.flock.translate`) and only constructs whose
translation was measured to agree are sent (the list below, from ``tests/test_flock_bigquery_conformance.py``
against a real BigQuery); the Pond can accept known, bounded differences by name. Every builder shape is
eligible.

Sources are staged as Parquet under the scratch prefix and read through temporary external tables in the scratch
dataset (dropped after each dispatch; the dataset's default table expiration is the backstop). The result is
written with ``EXPORT DATA`` as Parquet and read back by the Duck. Calls go over REST through google-auth's
authorized session, as the Cloud Run backend's do (``catchment/gcp_rest.py``).

Credentials: the scratch URI's ``credentials=`` (a service-account key, as for a ``gs://`` data root), else
application default credentials. The service account needs BigQuery Job User on the project, Data Editor on the
dataset, and object access to the scratch prefix.

Config (``dataset`` and ``scratch`` are required to enable):

- ``DUCKSTRING_FLOCK_BIGQUERY_DATASET``: the dataset for the temporary external tables. Its location must be
  the scratch bucket's.
- ``DUCKSTRING_FLOCK_BIGQUERY_SCRATCH``: a ``gs://`` prefix for staged sources and results.
- ``DUCKSTRING_FLOCK_BIGQUERY_PROJECT``: the project jobs run in (else the credentials' project).
- ``DUCKSTRING_FLOCK_BIGQUERY_LOCATION``: the dataset's location (else BigQuery infers it).
- ``DUCKSTRING_FLOCK_BIGQUERY_TIMEOUT``: seconds a dispatch may run (default 900).

Requires the ``gcp`` extra (google-auth) and sqlglot (``pip install 'duckstring[gcp,lineage]'``).
"""

from __future__ import annotations

import time

from .. import translate
from .sql_engine import SqlEngine

_API = "https://bigquery.googleapis.com/bigquery/v2"
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# ── What BigQuery is trusted with ──────────────────────────────────────────────
#
# Each construct below was run on DuckDB and, translated (sqlglot plus the rewrites below), on BigQuery over the
# same staged data (NULLs, zero, negatives, values exactly halfway, 1e300, 2^53 + 1, empty, padded and non-ASCII
# strings), and compared after conform: the battery and report are tests/test_flock_bigquery_conformance.py
# (python tests/test_flock_bigquery_conformance.py prints the full report). Run it after a sqlglot upgrade.
#
# Measured 2026-11-08 (BigQuery, australia-southeast1; sqlglot 30.22): these agreed exactly, or (KINDS) within
# float_order.
#
# Kept out, each for a measured reason:
#   CAST(DECIMAL AS VARCHAR)  BigQuery drops trailing zeros ('2.50' against '2.5').
#   CAST(DOUBLE AS VARCHAR)   formats differ ('0.0' against '0').
#   CAST(VARCHAR AS …)        DuckDB raises on a malformed string where an engine may not (the translator's rule).
#   % on DOUBLE               BigQuery's MOD takes integers and NUMERIC only.
#   first_value, sum() OVER (ORDER BY …)  BigQuery refuses NULLS LAST (DuckDB's default) in a RANGE frame.
#   median, regr_*            no BigQuery function of that name.
#   string_agg(… ORDER BY)    no translation of DuckDB's NULLS LAST order.
#   strftime                  format codes only partly shared.
ALLOWED = frozenset({
    # arithmetic (Div, CAST to an integer, and ln/log/exp/pow/sqrt are rewritten, see _rewrite)
    "Add", "Sub", "Mul", "Div", "IntDiv", "Mod", "Abs", "Floor", "Ceil", "Round", "Sign", "Greatest", "Least",
    "Nullif", "Pow", "Sqrt", "Ln", "Exp", "Log",
    # strings
    "Upper", "Lower", "Length", "Substring", "Concat", "DPipe", "Trim", "Replace", "Like", "ILike", "StartsWith",
    "Pad", "Reverse", "Left", "StrPosition", "Contains",
    # dates (Year, Month, Day, DayOfWeek and Quarter are rewritten as EXTRACT)
    "Year", "Month", "Day", "DayOfWeek", "Quarter", "TimestampTrunc", "DateTrunc", "Interval", "DateDiff",
    "Extract",
    # aggregates
    "Count", "Sum", "Avg", "Min", "Max", "VariancePop", "Variance", "StddevPop", "Stddev", "StddevSamp",
    "CovarPop", "Corr", "ArgMin", "ArgMax", "LogicalAnd", "LogicalOr", "BitwiseAndAgg", "BitwiseOrAgg", "Filter",
    # windows
    "RowNumber", "Rank", "DenseRank", "Lag", "Lead",
})
# float_order: floating-point results that differ in the last bits. Sum and Avg are exact on integer and DECIMAL
# input (FLOAT_KINDS); the statistics compute in floating point whatever the input, and ln/exp/pow/log aren't
# correctly rounded by every math library.
KINDS = {name: "float_order" for name in (
    "Sum", "Avg", "VariancePop", "Variance", "StddevPop", "Stddev", "StddevSamp", "CovarPop", "Corr", "Log", "Ln",
    "Exp", "Pow")}
FLOAT_KINDS = frozenset({"Sum", "Avg"})
_EXACT = "4503599627370496"  # 2^52: every DOUBLE this large is already a whole number
_PARTS = {"Year": "year", "Month": "month", "Day": "day", "Quarter": "quarter"}


def _type(node) -> str:
    t = getattr(node, "type", None)
    return t.this.name if t is not None else "UNKNOWN"


def _rewrite(tree, exp):
    """DuckDB's semantics spelled out in GoogleSQL where BigQuery's own differ:

    - ``a / b`` is DOUBLE division in DuckDB, giving ±inf or NaN for a zero divisor where BigQuery's ``/``
      raises: ``IEEE_DIVIDE`` over FLOAT64 is DuckDB's answer.
    - ``CAST(x AS <integer>)`` of a DOUBLE rounds half to even in DuckDB and half away from zero in BigQuery:
      spelled out.
    - ``ln``, ``log``, ``exp``, ``pow`` and ``sqrt`` return DOUBLE in DuckDB, where BigQuery keeps a NUMERIC
      argument's type (nine decimal places): the arguments are cast to DOUBLE.
    - ``year()``, ``month()``, ``day()`` and ``quarter()`` aren't BigQuery functions: ``EXTRACT``. ``dayofweek``
      counts Sunday as 0 in DuckDB and 1 in BigQuery.
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
        name = type(node).__name__
        if isinstance(node, exp.Div):
            a, b = node.this.sql("duckdb"), node.expression.sql("duckdb")
            return parse(f"ieee_divide(CAST(({a}) AS DOUBLE), CAST(({b}) AS DOUBLE))")
        if isinstance(node, exp.Cast):
            to = node.to.this.name if isinstance(node.to, exp.DataType) else ""
            if to not in translate._INT_TYPES or _type(node.this) not in ("FLOAT", "DOUBLE"):
                return node
            x = node.this.sql("duckdb")
            floor_x = f"CASE WHEN abs({x}) >= {_EXACT} THEN ({x}) ELSE floor({x}) END"
            body = f"CASE WHEN ({x}) - ({floor_x}) = 0.5 THEN 2 * round(({x}) / 2) ELSE round({x}) END"
            return parse(f"CAST({body} AS {node.to.sql('duckdb')})")
        if name in ("Ln", "Log", "Exp", "Pow", "Sqrt"):
            for key in ("this", "expression"):
                arg = node.args.get(key)
                if isinstance(arg, exp.Expression) and _type(arg) not in ("FLOAT", "DOUBLE"):
                    node.set(key, exp.Cast(this=exp.Paren(this=arg.copy()), to=exp.DataType.build("DOUBLE")))
            node.set("_duckstring", True)
            return node
        if name in _PARTS:
            return parse(f"extract({_PARTS[name]} FROM ({node.this.sql('duckdb')}))")
        if isinstance(node, exp.DayOfWeek):
            return parse(f"(extract(dayofweek FROM ({node.this.sql('duckdb')})) - 1)")
        return node

    return tree.transform(fix)


def _check(node, exp) -> str | None:
    """BigQuery's refusals beyond construct names."""
    if isinstance(node, exp.Cast) and isinstance(node.to, exp.DataType) and node.to.this.name in ("VARCHAR", "TEXT") \
            and _type(node.this) in ("DECIMAL", "BIGDECIMAL"):
        return "CAST of a DECIMAL to text isn't sent: BigQuery drops trailing zeros"
    return None


PROFILE = translate.Profile(
    dialect="bigquery",
    rewrite=_rewrite,
    allowed=ALLOWED,
    kinds=KINDS,
    float_kinds=FLOAT_KINDS,
    int_only=frozenset({"IntDiv", "Mod"}),
    check=_check,
    cast_to=frozenset({"BIGINT", "INT", "SMALLINT", "TINYINT", "DOUBLE", "DECIMAL", "VARCHAR", "TEXT", "DATE",
                       "TIMESTAMP", "TIMESTAMPNTZ"}),
)


class BigQueryError(RuntimeError):
    """A BigQuery call or job failed; the message is BigQuery's own (it never carries a credential)."""


class BigQueryEngine(SqlEngine):
    """A :class:`duckstring.flock.FlockEngine` on BigQuery."""

    PROFILE = PROFILE
    SECRETS: tuple[str, ...] = ()  # credentials come from the scratch URI or the Duck's own identity

    def __init__(self, env, name: str | None = None):
        super().__init__(env, name or "bigquery")
        self.dataset = self._setting("DATASET")
        self.project = self._setting("PROJECT")
        self.location = self._setting("LOCATION")
        self.timeout = int(self._setting("TIMEOUT") or 900)
        self._session_cache = None
        self._tables: dict[str, list[str]] = {}

    def _setting(self, key: str) -> str | None:
        return self.env.get(f"DUCKSTRING_FLOCK_BIGQUERY_{key}") or None

    def enabled(self) -> bool:
        return bool(self.dataset and self.scratch)

    # ── REST ──────────────────────────────────────────────────────────────────

    def _session(self):
        """An authorized session and the project jobs run in."""
        if self._session_cache is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            spec = getattr(self._storage(), "params", {}).get("credentials")
            if spec and spec.strip() != "anon":
                from google.oauth2 import service_account

                from ...storage import _service_account_info

                info = _service_account_info(spec)
                creds = service_account.Credentials.from_service_account_info(info, scopes=[_SCOPE])
                project = info.get("project_id")
            else:
                creds, project = google.auth.default(scopes=[_SCOPE])
            project = self.project or project
            if not project:
                raise BigQueryError("no project: set DUCKSTRING_FLOCK_BIGQUERY_PROJECT")
            self._session_cache = (AuthorizedSession(creds), project)
        return self._session_cache

    def _call(self, method: str, url: str, **kwargs) -> dict:
        session, _ = self._session()
        resp = session.request(method, url, timeout=60, **kwargs)
        if resp.status_code >= 300:
            try:
                message = resp.json().get("error", {}).get("message") or resp.text
            except ValueError:
                message = resp.text
            raise BigQueryError(f"{resp.status_code}: {(message or '').strip()[:400]}")
        return resp.json() if resp.content else {}

    def _query(self, sql: str) -> dict:
        """Run one statement to completion; its job statistics."""
        _, project = self._session()
        body = {"configuration": {"query": {"query": sql, "useLegacySql": False}}}
        if self.location:
            body["jobReference"] = {"projectId": project, "location": self.location}
        job = self._call("POST", f"{_API}/projects/{project}/jobs", json=body)
        ref = job["jobReference"]
        params = {"location": ref["location"]} if ref.get("location") else {}
        deadline = time.monotonic() + self.timeout
        delay = 0.25
        while job.get("status", {}).get("state") != "DONE":
            if time.monotonic() > deadline:
                self._call("POST", f"{_API}/projects/{project}/jobs/{ref['jobId']}/cancel", params=params)
                raise TimeoutError(f"bigquery job {ref['jobId']} did not finish in {self.timeout}s")
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
            job = self._call("GET", f"{_API}/projects/{project}/jobs/{ref['jobId']}", params=params)
        error = job["status"].get("errorResult")
        if error:
            raise BigQueryError(f"bigquery job failed: {error.get('message', error)}"[:400])
        return job.get("statistics", {})

    def _table(self, name: str) -> str:
        _, project = self._session()
        return f"`{project}.{self.dataset}.{name}`"

    # ── SqlEngine ─────────────────────────────────────────────────────────────

    def read_of(self, uri: str, columns: dict[str, str], tag: str, i: int) -> str:
        name = f"dsjob_{tag}_src{i}"
        self._tables.setdefault(tag, []).append(name)
        self._query(f"CREATE EXTERNAL TABLE {self._table(name)} "
                    f"OPTIONS (format = 'PARQUET', uris = ['{uri}/data.parquet'])")
        # BigQuery reads a Parquet timestamp as TIMESTAMP (an instant). Staged timestamps are wall-clock (a
        # TIMESTAMPTZ is staged as its UTC wall-clock), which is BigQuery's DATETIME.
        naive = [c for c, t in columns.items() if t == "TIMESTAMP"]
        if not naive:
            return self._table(name)
        replace = ", ".join(f"DATETIME(`{c}`) AS `{c}`" for c in naive)
        return f"(SELECT * REPLACE ({replace}) FROM {self._table(name)})"

    def execute(self, sql: str, out_uri: str, tag: str) -> dict:
        stats = self._query(f"EXPORT DATA OPTIONS (uri = '{out_uri}*.parquet', format = 'PARQUET', "
                            f"overwrite = true) AS {sql}")
        billed = int(stats.get("query", {}).get("totalBytesBilled") or 0)
        return {"billed_mb": round(billed / 1e6, 1)}

    def cleanup(self, tag: str) -> None:
        for name in self._tables.pop(tag, []):  # best effort; the dataset's table expiration is the backstop
            try:
                self._query(f"DROP EXTERNAL TABLE IF EXISTS {self._table(name)}")
            except Exception:  # noqa: BLE001
                pass
        try:
            self._storage().rmtree("stage", tag)
        except Exception:  # noqa: BLE001
            pass
