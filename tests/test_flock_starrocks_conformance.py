"""StarRocks conformance: what the StarRocks Flock engine may be trusted with, measured against a real
StarRocks (plans/flock-coverage.md, part 2).

Every candidate construct runs through the engine's real path (translated by sqlglot with the engine's
rewrites, sources staged as Parquet, run on StarRocks, the result read back and conformed) and is compared
with DuckDB computing the same job locally. The data covers the cases where engines usually part ways:
NULLs, zero (and -0.0), negatives, values exactly halfway, large values, empty and padded strings, and
non-ASCII text.

- Every construct the engine **allows** must agree exactly.
- Every construct it **accepts by kind** must agree within that kind's bound.
- Constructs **left out** are recorded with the divergence that keeps them out; one that starts agreeing is
  reported, so the list can grow from evidence.

Needs a StarRocks and an S3 API both can reach (CI: ``starrocks/allin1-ubuntu`` and MinIO on one Docker
network). Skipped unless ``DUCKSTRING_TEST_STARROCKS`` (``host:port``) and ``DUCKSTRING_TEST_S3`` are set;
``DUCKSTRING_TEST_STARROCKS_S3_ENDPOINT`` is the S3 endpoint as StarRocks sees it.

Run ``python tests/test_flock_starrocks_conformance.py`` for the full report.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from decimal import Decimal

import duckdb
import pytest

from duckstring import flock
from duckstring import trickle_io as T
from duckstring.core import Pond

pytestmark = [
    pytest.mark.timeout(600),
    pytest.mark.skipif(not (os.environ.get("DUCKSTRING_TEST_STARROCKS") and os.environ.get("DUCKSTRING_TEST_S3")),
                       reason="needs DUCKSTRING_TEST_STARROCKS and DUCKSTRING_TEST_S3"),
]

UTC = timezone.utc
BUCKET = "duckstring-flock"
KEY = os.environ.get("DUCKSTRING_TEST_S3_KEY", "duckstringtest")
SECRET = os.environ.get("DUCKSTRING_TEST_S3_SECRET", "duckstringtest")

# id, i (BIGINT), j (INTEGER), d (DECIMAL(10,2)), x (DOUBLE), s (VARCHAR), b (BOOLEAN), dt (DATE), ts (TIMESTAMP),
# g (group key)
ROWS = [
    (1, 7, 2, "2.50", 2.5, "Hello", True, "2026-01-31", "2026-01-31 23:59:59", "a"),
    (2, -7, 3, "-2.50", -2.5, "  padded  ", False, "2024-02-29", "2024-02-29 12:00:00", "a"),
    (3, 0, 0, "0.00", 0.0, "", None, "2000-01-01", "2000-01-01 00:00:00", "b"),
    (4, None, None, None, None, None, None, None, None, "b"),
    (5, 9007199254740993, -1, "99999999.99", 1e300, "Ünïcødé", True, "1999-12-31", "1999-12-31 06:30:00", "c"),
    (6, 3, 7, "3.50", 3.5, "a_b%c", False, "2026-10-11", "2026-10-11 05:06:07", "c"),
    (7, -3, 4, "-0.50", -0.0, "MiXeD case", True, "2026-03-29", "2026-03-29 02:30:00", "c"),
    (8, 42, 5, "1.05", 0.1, "x", True, "2026-12-31", "2026-12-31 23:00:00", "a"),
    (9, 2, 2, "0.15", 1.005, "tab\there", False, "2026-07-04", "2026-07-04 00:00:01", "b"),
]


def _lit(v):
    if v is None:
        return "NULL"
    return repr(v) if isinstance(v, str) else str(v)


SOURCE_SQL = (
    "SELECT CAST(id AS BIGINT) AS id, CAST(i AS BIGINT) AS i, CAST(j AS INTEGER) AS j, CAST(d AS DECIMAL(10,2)) AS d, "
    "CAST(x AS DOUBLE) AS x, CAST(s AS VARCHAR) AS s, CAST(b AS BOOLEAN) AS b, CAST(dt AS DATE) AS dt, "
    "CAST(ts AS TIMESTAMP) AS ts, CAST(g AS VARCHAR) AS g FROM (VALUES "
    + ", ".join("(" + ", ".join(_lit(v) for v in r) + ")" for r in ROWS)
    + ") v(id, i, j, d, x, s, b, dt, ts, g)"
)

# ── the candidates: construct → (kind of query, SQL) ────────────────────────────
# "row": a computed column per source row; "agg": an aggregate per group g; "win": a window over all rows.
ROW = {
    "Add": "s0.i + s0.j", "Sub": "s0.d - s0.j", "Mul": "s0.d * 1.5", "Mul_dec": "s0.d * CAST(s0.j AS DECIMAL(5,2))",
    "Mul_float": "s0.x * 3", "Div_int": "s0.i / nullif(s0.j, 0)", "Div_dec": "s0.d / 3",
    "Div_float": "s0.x / nullif(s0.j, 0)", "Div_zero_nan": "s0.i / s0.j",
    "Div_zero_inf": "(s0.i + 100) / (s0.j - s0.j)", "Div_zero_neginf": "(s0.i - 100000000000000000) / (s0.j - s0.j)",
    "IntDiv": "s0.i // 2", "IntDiv_neg_zero": "s0.i // s0.j", "IntDiv_dec": "s0.d // 2",
    "Mod": "s0.i % 4", "Mod_float": "s0.x % 2", "Neg": "-s0.d", "Abs": "abs(s0.d)", "Floor": "floor(s0.d)",
    "Ceil": "ceil(s0.d)", "Round": "round(s0.d)", "Round_2": "round(s0.d, 1)", "Round_float": "round(s0.x)",
    "Round_float_2": "round(s0.x, 1)", "Floor_float": "floor(s0.x)", "Ceil_float": "ceil(s0.x)",
    "Cast_float_int": "CAST(CASE WHEN abs(s0.x) < 1e9 THEN s0.x END AS INTEGER)",
    "Cast_varchar_date": "CAST(s0.s AS DATE)",
    "Sign": "sign(s0.i)", "Greatest": "greatest(s0.i, s0.j)", "Least": "least(s0.d, s0.j)",
    "Nullif": "nullif(s0.j, 2)", "Coalesce": "coalesce(s0.i, 0)", "Pow": "pow(s0.j, 2)",
    "Sqrt": "sqrt(abs(s0.x))", "Ln": "ln(abs(s0.d) + 1)", "Exp": "exp(s0.j)", "Log10": "log10(abs(s0.d) + 1)",
    "Case": "CASE WHEN s0.i > 0 THEN 'pos' WHEN s0.i < 0 THEN 'neg' ELSE 'zero' END",
    "In": "s0.j IN (2, 3)", "Between": "s0.d BETWEEN 0 AND 3", "Is": "s0.b IS NULL", "Not": "NOT s0.b",
    "Cast_dec_int": "CAST(s0.d AS INTEGER)", "Cast_float_bigint": "CAST(s0.x / 1e290 AS BIGINT)",
    "Cast_int_double": "CAST(s0.i AS DOUBLE)", "Cast_int_dec": "CAST(s0.j AS DECIMAL(12,3))",
    "Cast_int_varchar": "CAST(s0.i AS VARCHAR)", "Cast_dec_varchar": "CAST(s0.d AS VARCHAR)",
    "Cast_float_varchar": "CAST(s0.x AS VARCHAR)", "Cast_ts_date": "CAST(s0.ts AS DATE)",
    "Cast_date_varchar": "CAST(s0.dt AS VARCHAR)", "Cast_varchar_int": "CAST(s0.j || '' AS INTEGER)",
    "Upper": "upper(s0.s)", "Lower": "lower(s0.s)", "Length": "length(s0.s)", "Substring": "substring(s0.s, 2, 3)",
    "Concat": "concat(s0.s, '!')", "DPipe": "s0.s || '!'", "Trim": "trim(s0.s)", "LTrim": "ltrim(s0.s)",
    "RTrim": "rtrim(s0.s)", "Replace": "replace(s0.s, 'a', 'A')", "Like": "s0.s LIKE '%a%'",
    "ILike": "s0.s ILIKE '%A%'", "StartsWith": "starts_with(s0.s, 'H')", "Contains": "contains(s0.s, 'e')",
    "Lpad": "lpad(s0.s, 6, '*')", "Reverse": "reverse(s0.s)", "Left": "left(s0.s, 2)", "StrPosition": "strpos(s0.s, 'e')",
    "Year": "year(s0.dt)", "Month": "month(s0.ts)", "Day": "day(s0.dt)", "DayOfWeek": "dayofweek(s0.dt)",
    "Quarter": "quarter(s0.dt)", "DateTrunc": "date_trunc('month', s0.ts)", "DateTrunc_date": "date_trunc('month', s0.dt)",
    "DateAdd": "s0.dt + INTERVAL 1 DAY", "DateDiff": "date_diff('day', s0.dt, DATE '2026-01-01')",
    "Extract_hour": "extract(hour FROM s0.ts)", "StrfTime": "strftime(s0.ts, '%Y-%m')",
    "Ts_compare": "s0.ts > TIMESTAMP '2026-01-01 00:00:00'",
}
AGG = {
    "Count": "count(x)", "CountStar": "count(*)", "CountDistinct": "count(DISTINCT j)", "Sum_int": "sum(i)",
    "Sum_dec": "sum(d)", "Sum_float": "sum(x)", "Avg_int": "avg(j)", "Avg_dec": "avg(d)", "Avg_float": "avg(x)",
    "Min_str": "min(s)", "Max_dec": "max(d)", "VariancePop": "var_pop(d)", "Variance": "var_samp(d)",
    "StddevSamp": "stddev_samp(d)", "Stddev": "stddev(d)", "StddevPop": "stddev_pop(d)",
    "Covar": "covar_pop(d, j)", "Corr": "corr(d, j)",
    "RegrSlope": "regr_slope(d, j)", "RegrSxx": "regr_sxx(d, j)", "RegrCount": "regr_count(d, j)",
    "ArgMin": "arg_min(s, id)", "ArgMax": "arg_max(id, i)", "LogicalAnd": "bool_and(b)", "LogicalOr": "bool_or(b)",
    "BitwiseAndAgg": "bit_and(j)", "BitwiseOrAgg": "bit_or(j)", "Median": "median(d)", "SumIf": "sum(d) FILTER (WHERE j > 2)",
    "StringAgg": "string_agg(s, ',' ORDER BY id)",
}
WIN = {
    "RowNumber": "row_number() OVER (ORDER BY id)", "Rank": "rank() OVER (PARTITION BY g ORDER BY j)",
    "DenseRank": "dense_rank() OVER (PARTITION BY g ORDER BY j)", "Lag": "lag(d) OVER (ORDER BY id)",
    "Lead": "lead(s, 2) OVER (ORDER BY id)", "SumOver": "sum(d) OVER (PARTITION BY g ORDER BY id)",
    "FirstValue": "first_value(s) OVER (PARTITION BY g ORDER BY id)", "CountOver": "count(*) OVER (PARTITION BY g)",
}


def _env():
    host, _, port = os.environ["DUCKSTRING_TEST_STARROCKS"].partition(":")
    endpoint = os.environ["DUCKSTRING_TEST_S3"].rstrip("/")
    return {
        "DUCKSTRING_FLOCK_STARROCKS_HOST": host, "DUCKSTRING_FLOCK_STARROCKS_PORT": port or "9030",
        "DUCKSTRING_FLOCK_STARROCKS_USER": os.environ.get("DUCKSTRING_TEST_STARROCKS_USER", "root"),
        "DUCKSTRING_FLOCK_STARROCKS_SCRATCH": f"s3://{BUCKET}/scratch?key_id={KEY}&secret={SECRET}&region=us-east-1",
        "DUCKSTRING_FLOCK_STARROCKS_REGION": "us-east-1",
        "DUCKSTRING_FLOCK_STARROCKS_S3_ENDPOINT": os.environ.get("DUCKSTRING_TEST_STARROCKS_S3_ENDPOINT", endpoint),
        "STARROCKS_S3_ACCESS_KEY": KEY, "STARROCKS_S3_SECRET_KEY": SECRET,
        "DUCKSTRING_S3_ENDPOINT": endpoint,
    }


def _setup(tmp_path):
    import boto3

    env = _env()
    os.environ["DUCKSTRING_S3_ENDPOINT"] = env["DUCKSTRING_S3_ENDPOINT"]
    s3 = boto3.client("s3", endpoint_url=env["DUCKSTRING_S3_ENDPOINT"], region_name="us-east-1",
                      aws_access_key_id=KEY, aws_secret_access_key=SECRET)
    try:
        s3.create_bucket(Bucket=BUCKET)
    except Exception:  # noqa: BLE001 (already there)
        pass
    from duckstring.flock.engines.starrocks import StarRocksEngine

    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    pond = Pond("p", "1", con, root=tmp_path, f=datetime(2026, 10, 11, tzinfo=UTC), flock="off")
    T.merge_table(con, "t", con.sql(SOURCE_SQL), pond.f, ("id",))
    return pond, StarRocksEngine(env)


def _builder(pond, kind, expr):
    t = pond.trickle("t")
    if kind == "row":
        return t.select(f"s0.id, {expr} AS v"), ("id",)
    if kind == "agg":
        return t.alias("t").sql(f"SELECT g, {expr} AS v FROM t GROUP BY g"), ("g",)
    return t.alias("t").sql(f"SELECT id, {expr} AS v FROM t"), ("id",)


def _equal(a, b, *, float_order=False):
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        if a == b:
            return True  # -0.0 equals 0.0, as in SQL
        return float_order and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-300)
    return a == b and type(a) is type(b) or (isinstance(a, Decimal) and isinstance(b, Decimal) and a == b)


def measure(pond, engine, kind, expr, *, float_order=False):
    """``(verdict, detail)``: ``"agree"``, ``"diverge"`` (with the first differing rows), ``"error"`` (the engine
    or translation failed: the job would stay local, which is safe), or ``"local-error"`` (DuckDB itself
    raises, so there is nothing to compare)."""
    from trickle.builder import ComprehensiveJob

    builder, pk = _builder(pond, kind, expr)
    job = ComprehensiveJob(builder, pk)
    try:
        local = sorted(job.relation().fetchall(), key=repr)
    except Exception as exc:  # noqa: BLE001
        return "local-error", f"{type(exc).__name__}: {str(exc)[:120]}"
    engine.measuring = True
    engine._translations.clear()
    result = engine.dispatch(job)
    if result is None:
        return "error", engine.last_error
    remote = flock.conform(job, result)
    if remote is None:
        return "error", "did not conform"
    remote = sorted(remote.fetchall(), key=repr)
    diffs = [(lr, rr) for lr, rr in zip(local, remote, strict=False)
             if not all(_equal(a, b, float_order=float_order) for a, b in zip(lr, rr, strict=True))]
    if len(local) != len(remote) or diffs:
        return "diverge", (diffs[:3] or f"{len(local)} rows locally, {len(remote)} remotely")
    return "agree", None


CANDIDATES = [("row", k, v) for k, v in ROW.items()] + [("agg", k, v) for k, v in AGG.items()] + \
             [("win", k, v) for k, v in WIN.items()]


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    return _setup(tmp_path_factory.mktemp("sr"))


def _construct_names(pond, engine, kind, expr):
    """The sqlglot constructs the candidate's job contains beyond the query skeleton."""
    import sqlglot
    from sqlglot import exp

    from duckstring.flock import translate

    builder, pk = _builder(pond, kind, expr)
    from trickle.builder import ComprehensiveJob

    sql = ComprehensiveJob(builder, pk).sql(lambda leaf: 'SELECT * FROM "t"')
    names = set()
    for node in sqlglot.parse_one(sql, read="duckdb").walk():
        name = f"fn:{str(node.this).lower()}" if isinstance(node, exp.Anonymous) else type(node).__name__
        if name not in translate.STRUCTURAL:
            names.add(name)
    return names


@pytest.mark.parametrize("kind,label,expr", CANDIDATES, ids=[c[1] for c in CANDIDATES])
def test_every_allowed_candidate_agrees(setup, kind, label, expr):
    """A candidate the engine would send (its constructs all allowed, or accepted as float_order) must agree;
    one it refuses is skipped here and listed by the report."""
    from duckstring.flock import translate
    from duckstring.flock.engines.starrocks import PROFILE

    pond, engine = setup
    builder, pk = _builder(pond, kind, expr)
    from trickle.builder import ComprehensiveJob

    job = ComprehensiveJob(builder, pk)
    t = translate.translate(job, PROFILE, ("float_order",))
    if t.reason is not None:
        pytest.skip(f"not sent: {t.reason}")
    verdict, detail = measure(pond, engine, kind, expr, float_order=True)
    # A deliberate abort (a value StarRocks can't represent) runs the job locally: safe, not a divergence.
    deliberate = verdict == "error" and "cannot reproduce" in str(detail)
    assert verdict in ("agree", "local-error") or deliberate, f"{label}: {verdict} {detail}"


if __name__ == "__main__":  # the full report
    import tempfile
    from pathlib import Path

    from trickle.builder import ComprehensiveJob

    from duckstring.flock import translate
    from duckstring.flock.engines.starrocks import PROFILE

    pond, engine = _setup(Path(tempfile.mkdtemp()))
    for kind, label, expr in CANDIDATES:
        verdict, detail = measure(pond, engine, kind, expr)
        if verdict == "diverge":
            fo, _ = measure(pond, engine, kind, expr, float_order=True)
            verdict = "float_order" if fo == "agree" else verdict
        b, pk = _builder(pond, kind, expr)
        sent = translate.translate(ComprehensiveJob(b, pk), PROFILE, ("float_order",)).reason is None
        print(f"{label:20} {verdict:12} {'SENT' if sent else 'kept':5} {detail if verdict != 'agree' else ''}")


# ── whole builder jobs through the engine's normal path ──────────────────────────


def _shapes(p):
    from duckstring import agg

    t, u = (lambda: p.trickle("t")), (lambda: p.trickle("u"))
    return {
        "inner": (t().join(u(), on="g").select("s0.id, s1.label, s0.d * s1.w AS dw"), "id"),
        "left_mutate": (t().join(u(), on="g", how="left").mutate(r="s0.i / s1.w", c="CAST(s0.d AS INTEGER)")
                        .filter("coalesce(r, 0) >= -10").select("s0.id, r, c, s1.label"), "id"),
        "right": (t().alias("a").join(u().alias("b"), on="g", how="right")
                  .mutate(k="coalesce(CAST(a.id AS VARCHAR), 'none-' || b.g)").select("k, a.s, b.w"), "k"),
        "full": (t().alias("a").join(u().alias("b"), on="g", how="full")
                 .mutate(k="coalesce(CAST(a.id AS VARCHAR), 'none-' || b.g)").select("k, a.j, b.label"), "k"),
        "semi": (t().join(u(), on="g", how="semi").select("s0.id, s0.s"), "id"),
        "anti": (t().join(u(), on="g", how="anti").select("s0.id, s0.dt"), "id"),
        "aggregate": (t().join(u(), on="g").select("s0.id, s0.g, s0.b, s0.d, s0.x, s1.w")
                      .aggregate("g", n=agg.count(), s=agg.sum("d"), m=agg.mean("d"), v=agg.var("d"),
                                 lo=agg.min("d"), hi=agg.max("x"), wa=agg.weighted_average("d", "w"),
                                 first=agg.argmin("id", "id"), anyb=agg.bool_or("b"), prod=agg.product("w")), "g"),
        "sql_windows": (t().alias("t").sql("SELECT id, g, d, rank() OVER (PARTITION BY g ORDER BY d, id) AS rk, "
                                           "sum(d) OVER (PARTITION BY g) AS gs FROM t")
                        .alias("r").sql("SELECT id, rk, gs FROM r WHERE rk <= 2"), "id"),
    }


@pytest.fixture(scope="module")
def shape_setup(tmp_path_factory):
    pond, engine = _setup(tmp_path_factory.mktemp("srshapes"))
    T.merge_table(pond.con, "u", pond.con.sql(
        "SELECT * FROM (VALUES ('a', 'Alpha', 2.0), ('b', 'Beta', 0.5), ('z', 'Zed', 3.0)) v(g, label, w)"),
        pond.f, ("g",))
    return pond, engine


@pytest.mark.parametrize("shape", ["inner", "left_mutate", "right", "full", "semi", "anti", "aggregate", "sql_windows"])
def test_builder_jobs_are_sent_and_agree(shape_setup, shape):
    from trickle.builder import ComprehensiveJob

    pond, engine = shape_setup
    engine.measuring = False
    engine.accept = ("float_order",)
    builder, pk = _shapes(pond)[shape]
    job = ComprehensiveJob(builder, (pk,), "aggregate" if shape == "aggregate" else "rows")
    engine._translations.clear()
    assert engine.eligible(job) is None, engine.eligible(job)
    local = sorted(job.relation().fetchall(), key=repr)
    result = flock._dispatch(engine, job, {"DUCKSTRING_FLOCK_VERIFY_KEYS": "1000"})  # conform + verification
    assert result is not None, flock.stats()
    remote = sorted(result.fetchall(), key=repr)
    assert len(local) == len(remote) and local
    for lr, rr in zip(local, remote, strict=True):
        assert all(_equal(a, b, float_order=True) for a, b in zip(lr, rr, strict=True)), (lr, rr)


def test_float_order_is_needed_for_a_floating_point_aggregate(shape_setup):
    from trickle.builder import ComprehensiveJob

    from duckstring import agg

    pond, engine = shape_setup
    engine.accept = ()
    engine._translations.clear()
    job = ComprehensiveJob(pond.trickle("t").aggregate("g", v=agg.var("d")), ("g",), "aggregate")
    assert "float_order" in (engine.eligible(job) or "")
    # count/min/max are exact. (A sum also keeps the centred second moment for variance, computed in floating
    # point, so any sum or mean needs float_order on a SQL engine.)
    exact = ComprehensiveJob(pond.trickle("t").aggregate("g", n=agg.count(), lo=agg.min("d"), hi=agg.max("s")),
                             ("g",), "aggregate")
    assert engine.eligible(exact) is None
