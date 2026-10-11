"""The StarRocks Flock engine (plans/flock-coverage.md): a job translated to StarRocks SQL, for recomputes past
what one DuckDB machine handles.

StarRocks isn't DuckDB, so a job is translated (:mod:`duckstring.flock.translate`) and only constructs whose
translation was measured to agree are sent (the list below, from ``tests/test_flock_starrocks_conformance.py``
against a real StarRocks); the Pond can accept known, bounded differences by name. Everything shape-related
(every join, filters, computed columns, ``.sql()`` queries, an aggregate's rebuild) is eligible.

Sources are staged as Parquet under the scratch prefix and read with ``FILES()``, which infers the schema, so
there is no DDL per dispatch. The result is written with ``INSERT INTO FILES`` (StarRocks 3.2+) and read back
by the Duck. StarRocks reads and writes the scratch prefix with its own credentials: an instance profile by
default, or keys from the secrets ``STARROCKS_S3_ACCESS_KEY``/``STARROCKS_S3_SECRET_KEY``.

Config (``host``, ``user`` and ``scratch`` are required to enable):

- ``DUCKSTRING_FLOCK_STARROCKS_HOST`` / ``_PORT`` (default 9030) / ``_USER``: the MySQL-protocol frontend.
- ``DUCKSTRING_FLOCK_STARROCKS_WAREHOUSE``: the warehouse to run in (CelerData: a dedicated one with
  auto-suspend, since dispatches are rare).
- ``DUCKSTRING_FLOCK_STARROCKS_SCRATCH``: an ``s3://`` prefix for staged sources and results; expire it with
  a lifecycle rule as a backstop to the per-dispatch cleanup.
- ``DUCKSTRING_FLOCK_STARROCKS_REGION``: the scratch bucket's region.
- ``DUCKSTRING_FLOCK_STARROCKS_S3_ENDPOINT``: the scratch endpoint as StarRocks sees it (an S3-compatible
  store); the Duck's own view comes from ``DUCKSTRING_S3_ENDPOINT``.
- ``DUCKSTRING_FLOCK_STARROCKS_TIMEOUT``: seconds a dispatch may run (default 900), including a suspended
  warehouse resuming.
- the secret ``STARROCKS_PASSWORD``.

Requires ``pymysql`` and ``sqlglot`` (``pip install 'duckstring[starrocks]'``).
"""

from __future__ import annotations

from .. import translate
from .sql_engine import SqlEngine

# ── What StarRocks is trusted with ──────────────────────────────────────────────
#
# Each construct below was run on DuckDB and, translated (sqlglot plus the rewrites and dialect fixes below), on
# StarRocks 4.1 over the same staged data (NULLs, zero, negatives, values exactly halfway, 1e300, 2^53 + 1,
# empty, padded and non-ASCII strings), and compared after conform. The battery and the report are
# tests/test_flock_starrocks_conformance.py; run it after a StarRocks or sqlglot upgrade.
#
# Measured 2026-10-11 (StarRocks 4.1.6; sqlglot 28.5 and 30.21): these agreed exactly, or (KINDS) within
# float_order.
#
# Kept out, each for a measured reason:
#   upper/lower        ASCII-only on StarRocks ('Ünï' stays 'ÜNï').
#   CAST(DOUBLE AS VARCHAR)  formats differ ('0.0' against '0').
#   CAST(VARCHAR AS …) DuckDB raises on a malformed string where StarRocks returns NULL.
#   median             interpolates differently on DECIMAL.
#   strftime           format codes only partly shared.
#   regr_*, contains, bit_and/bit_or  no StarRocks function of that signature (the job would fail remotely, so
#                      they are refused up front). An aggregate's co-moment metrics use regr_*, so covariance,
#                      correlation and OLS keep an aggregate local here.
#   `//` on non-integers, round(DOUBLE, n>0)  unmeasured.
ALLOWED = frozenset({
    # arithmetic (Div, IntDiv, Round/Floor/Ceil on DOUBLE and CAST to an integer are rewritten, see _rewrite)
    "Add", "Sub", "Mul", "Div", "IntDiv", "Mod", "Abs", "Floor", "Ceil", "Round", "Sign", "Greatest", "Least",
    "Nullif", "Pow", "Sqrt", "Ln", "Exp", "Log",
    # strings
    "Length", "Substring", "Concat", "DPipe", "Trim", "Replace", "Like", "ILike", "StartsWith", "Pad",
    "Reverse", "Left", "StrPosition",
    # dates
    "Year", "Month", "Day", "DayOfWeek", "Quarter", "TimestampTrunc", "Interval", "DateDiff", "Extract",
    # aggregates
    "Count", "Sum", "Avg", "Min", "Max", "VariancePop", "Variance", "StddevPop", "Stddev", "StddevSamp",
    "CovarPop", "Corr", "ArgMin", "ArgMax", "LogicalAnd", "LogicalOr", "Filter", "GroupConcat",
    # windows
    "RowNumber", "Rank", "DenseRank", "Lag", "Lead", "FirstValue",
})
# float_order: floating-point results that differ in the last bits. Sum and Avg are exact on integer and DECIMAL
# input (FLOAT_KINDS); the statistics compute in floating point whatever the input, and ln/exp/pow/log aren't
# correctly rounded by every math library (measured: log10 differed by one ulp).
KINDS = {name: "float_order" for name in (
    "Sum", "Avg", "VariancePop", "Variance", "StddevPop", "Stddev", "StddevSamp", "CovarPop", "Corr", "Log", "Ln",
    "Exp", "Pow")}
FLOAT_KINDS = frozenset({"Sum", "Avg"})


def _dialect():
    """sqlglot's StarRocks dialect with what it gets wrong for a DuckDB query put right. Registered with
    sqlglot as ``duckstringstarrocks``.

    - FULL OUTER JOIN is kept (StarRocks has it) rather than emulated with a UNION, as for MySQL.
    - ``var_samp``/``stddev`` are DuckDB's sample statistics, but MySQL's ``VARIANCE``/``STDDEV`` (which sqlglot
      emits) are the population ones: spelled ``VAR_SAMP``/``STDDEV_SAMP``.
    - ``arg_min``/``arg_max`` are StarRocks' ``MIN_BY``/``MAX_BY``.
    - DuckDB's integer ``//`` truncates toward zero and gives NULL on zero, as StarRocks' ``DIV`` does.
    """
    from sqlglot import exp, transforms
    from sqlglot.dialects.dialect import rename_func
    from sqlglot.dialects.starrocks import StarRocks

    class DuckstringStarrocks(StarRocks):
        class Generator(StarRocks.Generator):
            TRANSFORMS = {
                **StarRocks.Generator.TRANSFORMS,
                exp.Select: transforms.preprocess([
                    transforms.eliminate_distinct_on,
                    transforms.eliminate_semi_and_anti_joins,
                    transforms.eliminate_qualify,
                ]),
                exp.Variance: rename_func("VAR_SAMP"),
                exp.Stddev: rename_func("STDDEV_SAMP"),
                exp.ArgMin: rename_func("MIN_BY"),
                exp.ArgMax: rename_func("MAX_BY"),
                exp.IntDiv: lambda self, e: f"({self.sql(e, 'this')} DIV {self.sql(e, 'expression')})",
            }

    return DuckstringStarrocks


_INF = "(1e308 * 10)"  # StarRocks has no infinity literal; an overflowing product is +inf
_EXACT = "4503599627370496"  # 2^52: every DOUBLE this large is already a whole number


def _type(node) -> str:
    t = getattr(node, "type", None)
    return t.this.name if t is not None else "UNKNOWN"


def _rewrite(tree, exp):
    """DuckDB's semantics spelled out in StarRocks SQL where StarRocks' own differ:

    - ``a / b`` is true division to DOUBLE in DuckDB, where StarRocks keeps DECIMAL scale. Dividing by zero
      gives ±inf in DuckDB (NULL in StarRocks): reproduced for an integer or DECIMAL divisor. NaN (``0/0``)
      can't be represented on StarRocks, and a DOUBLE zero divisor may be -0.0, so a row with either raises
      on StarRocks, which fails the dispatch and runs the job locally rather than publish a different value.
    - ``CAST(x AS <integer>)`` rounds in DuckDB (DECIMAL half away from zero, DOUBLE half to even) where
      StarRocks truncates: round first, spelling out half-to-even for DOUBLE.
    - ``round``/``floor``/``ceil`` of a DOUBLE return BIGINT on StarRocks, clamping large values: kept DOUBLE.
    - ``dayofweek`` counts Sunday as 0 in DuckDB and 1 in StarRocks.
    """
    import sqlglot

    _ensure_dialect()

    def parse(sql):
        out = sqlglot.parse_one(sql, read="duckdb")
        for node in out.walk():
            node.set("_duckstring", True)
        return out

    def floor_of(x):
        return f"CASE WHEN abs({x}) >= {_EXACT} THEN ({x}) ELSE floor({x}) END"

    def fix(node):
        if node.args.get("_duckstring"):
            return node
        if isinstance(node, exp.Div):
            a, b = f"CAST(({node.this.sql('duckdb')}) AS DOUBLE)", f"CAST(({node.expression.sql('duckdb')}) AS DOUBLE)"
            # The zero divisions StarRocks can't reproduce: 0/0 (NaN), and any zero DOUBLE divisor (it may be
            # -0.0, which flips the infinity's sign). assert_true raises only for a row that has one; StarRocks
            # evaluates CASE branches eagerly, so a raise inside a branch would fire on every row.
            unrepresentable = f"{b} = 0 AND {a} = 0" if _type(node.expression) not in ("FLOAT", "DOUBLE", "UNKNOWN") \
                else f"{b} = 0 AND {a} IS NOT NULL"
            return parse(
                f"IF(assert_true(coalesce(NOT ({unrepresentable}), true), "
                f"'duckstring: a division by zero StarRocks cannot reproduce'), "
                f"CASE WHEN {b} = 0 THEN CASE WHEN {a} > 0 THEN {_INF} WHEN {a} < 0 THEN -{_INF} END "
                f"ELSE {a} / {b} END, NULL)")
        if isinstance(node, exp.Cast):
            to = node.to.this.name if isinstance(node.to, exp.DataType) else ""
            src = _type(node.this)
            if to not in translate._INT_TYPES or src in translate._INT_TYPES:
                return node
            x = node.this.sql("duckdb")
            if src.startswith("DECIMAL") or src == "BIGDECIMAL":
                body = f"round({x})"
            else:
                body = f"CASE WHEN ({x}) - ({floor_of(x)}) = 0.5 THEN 2 * round(({x}) / 2, 0) ELSE round({x}, 0) END"
            return parse(f"CAST({body} AS {node.to.sql('duckdb')})")
        if isinstance(node, (exp.Round, exp.Floor, exp.Ceil)) and _type(node.this) in ("FLOAT", "DOUBLE"):
            x = node.this.sql("duckdb")
            if isinstance(node, exp.Round):
                if node.args.get("decimals") is not None:
                    return node
                return parse(f"round({x}, 0)")
            fn = "floor" if isinstance(node, exp.Floor) else "ceil"
            return parse(f"CASE WHEN abs({x}) >= {_EXACT} THEN ({x}) ELSE CAST({fn}({x}) AS DOUBLE) END")
        if isinstance(node, exp.DayOfWeek):
            return parse(f"(dayofweek({node.this.sql('duckdb')}) - 1)")
        return node

    return tree.transform(fix)


def _check(node, exp) -> str | None:
    """StarRocks' refusals beyond construct names."""
    if isinstance(node, exp.Round) and node.args.get("decimals") is not None and _type(node.this) in (
            "FLOAT", "DOUBLE", "UNKNOWN"):
        return "round() of a DOUBLE to decimal places hasn't been measured equal on StarRocks"
    return None


_DIALECT = None


def _ensure_dialect():
    global _DIALECT
    if _DIALECT is None:
        _DIALECT = _dialect()
    return _DIALECT


PROFILE = translate.Profile(
    dialect="duckstringstarrocks",
    rewrite=_rewrite,
    allowed=ALLOWED,
    kinds=KINDS,
    float_kinds=FLOAT_KINDS,
    int_only=frozenset({"IntDiv"}),
    check=_check,
    cast_to=frozenset({"BIGINT", "INT", "SMALLINT", "TINYINT", "DOUBLE", "DECIMAL", "VARCHAR", "TEXT", "DATE",
                       "TIMESTAMP", "TIMESTAMPNTZ"}),
)


def _lit(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class StarRocksEngine(SqlEngine):
    PROFILE = PROFILE
    SECRETS: tuple[str, ...] = ("STARROCKS_PASSWORD", "STARROCKS_S3_ACCESS_KEY", "STARROCKS_S3_SECRET_KEY")

    def __init__(self, env, name: str | None = None):
        _ensure_dialect()
        super().__init__(env, name or "starrocks")
        self.host = self._setting("HOST")
        self.port = int(self._setting("PORT") or 9030)
        self.user = self._setting("USER")
        self.warehouse = self._setting("WAREHOUSE")
        self.region = self._setting("REGION")
        self.endpoint = self._setting("S3_ENDPOINT")
        self.timeout = int(self._setting("TIMEOUT") or 900)
        self._password = env.get("STARROCKS_PASSWORD") or ""
        self._key = env.get("STARROCKS_S3_ACCESS_KEY")
        self._secret = env.get("STARROCKS_S3_SECRET_KEY")

    def _setting(self, key: str) -> str | None:
        return self.env.get(f"DUCKSTRING_FLOCK_STARROCKS_{key}") or None

    def enabled(self) -> bool:
        return bool(self.host and self.user and self.scratch)

    # ── SqlEngine ─────────────────────────────────────────────────────────────

    def _s3_properties(self) -> list[str]:
        props = []
        if self._key and self._secret:
            props += [f'"aws.s3.access_key" = {_lit(self._key)}', f'"aws.s3.secret_key" = {_lit(self._secret)}']
        else:
            props.append('"aws.s3.use_instance_profile" = "true"')
        if self.region:
            props.append(f'"aws.s3.region" = {_lit(self.region)}')
        if self.endpoint:
            props += [f'"aws.s3.endpoint" = {_lit(self.endpoint)}', '"aws.s3.enable_path_style_access" = "true"']
        return props

    def read_of(self, uri: str, columns: dict[str, str], tag: str, i: int) -> str:
        props = [f'"path" = {_lit(uri + "/data.parquet")}', '"format" = "parquet"'] + self._s3_properties()
        # A subquery, because StarRocks can't qualify columns by a FILES() alias.
        return f"(SELECT * FROM FILES({', '.join(props)}))"

    def execute(self, sql: str, out_uri: str, tag: str) -> dict:
        import pymysql

        props = [f'"path" = {_lit(out_uri)}', '"format" = "parquet"'] + self._s3_properties()
        con = pymysql.connect(host=self.host, port=self.port, user=self.user, password=self._password,
                              autocommit=True, connect_timeout=30, read_timeout=self.timeout + 60)
        try:
            with con.cursor() as cur:
                cur.execute(f"SET query_timeout = {self.timeout}")
                cur.execute("SET time_zone = '+00:00'")  # staged timestamps are UTC wall-clock
                if self.warehouse:
                    cur.execute(f"SET warehouse = {_lit(self.warehouse)}")
                cur.execute(f"INSERT INTO FILES({', '.join(props)}) {sql}")
                return {"rows": cur.rowcount}
        finally:
            con.close()

    def cleanup(self, tag: str) -> None:
        try:
            self._storage().rmtree("stage", tag)
        except Exception:  # noqa: BLE001 (the scratch lifecycle rule is the backstop)
            pass
