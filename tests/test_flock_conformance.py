"""Flock conformance: **DuckDB is the authority**.

Dispatching decides *where* a comprehensive recompute runs, never *what* gets published. So an engine's
result must be indistinguishable from DuckDB's — same columns, same order, same types, same values — and
anything we cannot prove equivalent must not be dispatched at all. Three layers enforce that, and this
file is the gate for each:

1. ``flock.semantic_reason`` — only expression forms proven equivalent are eligible (the whitelist).
2. ``flock.conform`` — the engine's result is cast to DuckDB's own schema for the terminal, or rejected.
3. the fallback contract — a rejected result degrades to local compute and is COUNTED as a failure, so a
   non-conformant engine is visible on /metrics rather than silently in your data.

The misbehaving engines below are the point: a fake that simply re-runs DuckDB proves nothing. Each one
reproduces a real cross-engine divergence (a widened decimal, a reordered projection, a dropped column)
and asserts we either normalise it or refuse it — never publish it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from duckstring import flock
from duckstring.core import Pond

pytestmark = pytest.mark.timeout(10)

UTC = timezone.utc
FLOCK_ENV = {
    "DUCKSTRING_FLOCK_ATHENA_WORKGROUP": "wg",
    "DUCKSTRING_FLOCK_ATHENA_DATABASE": "db",
    "DUCKSTRING_FLOCK_ATHENA_SCRATCH": "s3://bucket/scratch",
}


@pytest.fixture(autouse=True)
def _reset_stats():
    flock._stats.update({"dispatched": 0, "failed": 0, "last_error": None})
    yield


def _pond(tmp_path, flock_mode="always"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    return Pond(name="p", version="1", con=con, root=tmp_path,
                f=datetime(2026, 7, 19, tzinfo=UTC), flock=flock_mode)


def _seed(pond):
    pond.merge_table("src", pond.con.sql(
        "SELECT range AS id, (range % 7) AS grp, CAST(range AS DECIMAL(10,2)) AS amount FROM range(50)"
    ), pk="id")


class _Engine:
    """Runs the query on DuckDB (so values are right by construction) and then applies ``distort`` — the
    way a different engine would differ. ``None`` = a faithful engine."""

    last_error = None

    def __init__(self, distort=None):
        self.distort, self.dispatched = distort, False

    def enabled(self):
        return True

    def eligible(self, job):
        return None

    def estimate_rows(self, job):
        return 10_000_000

    def dispatch(self, job):
        self.dispatched = True
        rel = job.builder._full_join()
        return self.distort(rel) if self.distort else rel


def _run(tmp_path, monkeypatch, engine, projection="s0.id, s0.grp, s0.amount"):
    for k, v in FLOCK_ENV.items():
        monkeypatch.setenv(k, v)
    pond = _pond(tmp_path)
    _seed(pond)
    monkeypatch.setattr(flock, "get_engine", lambda env=None: engine)
    pond.trickle("src").select(projection).merge("out", pk="id")
    return pond


def _published(pond):
    """The published output as (schema, rows) — what a consumer would actually see."""
    from duckstring import trickle_io as T

    rel = T.current_state(pond.con, "out")
    schema = {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
    rows = rel.project("id, grp, amount").order("id").fetchall()
    return schema, rows


# ─── the oracle: a faithful engine must be indistinguishable from local compute ──


def test_a_faithful_engine_publishes_exactly_what_duckdb_would(tmp_path, monkeypatch):
    """The baseline the whole tier rests on: dispatching must be invisible in the output. Same schema,
    same column order, same rows as the identical Pond computed locally."""
    engine = _Engine()
    dispatched = _run(tmp_path / "dispatched", monkeypatch, engine)
    assert engine.dispatched

    monkeypatch.setattr(flock, "get_engine", lambda env=None: None)  # Flock off → the local path
    local = _run(tmp_path / "local", monkeypatch, None)

    assert _published(dispatched) == _published(local), "dispatch changed the published result"


# ─── the divergences that actually happen ───────────────────────────────────────


def test_a_widened_decimal_is_normalised_to_the_duckdb_type(tmp_path, monkeypatch):
    """The live incident's shape: an engine returns DECIMAL(36,2) where DuckDB says DECIMAL(35,2). The
    values are fine; the TYPE is not ours to change, because the published schema is the version
    contract. Conform casts it back rather than letting it reach the contract."""
    engine = _Engine(lambda rel: rel.project(
        "id, grp, CAST(amount AS DECIMAL(30,4)) AS amount"))
    pond = _run(tmp_path, monkeypatch, engine)
    schema, _rows = _published(pond)
    assert schema["amount"] == "DECIMAL(10,2)", f"engine type leaked into the published schema: {schema}"
    assert flock.stats()["dispatched"] == 1 and flock.stats()["failed"] == 0


def test_a_reordered_projection_is_restored_to_the_duckdb_order(tmp_path, monkeypatch):
    engine = _Engine(lambda rel: rel.project("amount, grp, id"))
    pond = _run(tmp_path, monkeypatch, engine)
    schema, rows = _published(pond)
    assert list(schema) == ["id", "grp", "amount"], f"column order came from the engine: {list(schema)}"
    assert rows[0] == (0, 0, pytest.approx(0)) or rows[0][0] == 0


def test_a_dropped_column_is_refused_not_published(tmp_path, monkeypatch):
    """A different column set means the engine computed a different query. Nothing to normalise — refuse
    it, run local, and count the failure so it surfaces."""
    engine = _Engine(lambda rel: rel.project("id, grp"))
    pond = _run(tmp_path, monkeypatch, engine)
    schema, _ = _published(pond)
    assert set(schema) == {"id", "grp", "amount"}, "the local result must have been used"
    assert flock.stats()["failed"] == 1
    assert "conform" in (flock.stats()["last_error"] or "").lower()


def test_an_extra_column_is_refused(tmp_path, monkeypatch):
    engine = _Engine(lambda rel: rel.project("id, grp, amount, 1 AS sneaky"))
    _run(tmp_path, monkeypatch, engine)
    assert flock.stats()["failed"] == 1


# ─── the allow-list: what is allowed to be dispatched at all ─────────────────────


@pytest.fixture
def _wide(tmp_path):
    """A source with the columns the expressions below use."""
    pond = _pond(tmp_path)
    pond.merge_table("w", pond.con.sql(
        "SELECT range AS id, range AS a, range + 1 AS b, CAST(range AS DECIMAL(10,2)) AS q, "
        "CAST(range AS DECIMAL(10,2)) AS p, 'n' || range AS name FROM range(5)"), pk="id")
    return pond


def _athena_reason(pond, expr_kind, expr):
    """Athena's own verdict. Equivalence is a property of the expression AND the engine that runs it, so
    the list lives with the engine rather than in a universal 'safe SQL' notion."""
    from trickle.builder import ComprehensiveJob

    from duckstring.flock import translate
    from duckstring.flock.engines.athena import PROFILE

    b = pond.trickle("w")
    b = b.select(f"s0.id, {expr}") if expr_kind == "select" else b.filter(expr)
    return translate.translate(ComprehensiveJob(b, ("id",)), PROFILE).reason


@pytest.mark.parametrize("expr", [
    "s0.a, s0.b",                          # bare columns
    "s0.a AS x, s0.b AS y",                # renamed
    "s0.q * s0.p AS gross",                # decimal arithmetic: same result-type rule, exact
    "round(s0.q * s0.p, 2) AS r",          # both round half away from zero
    "coalesce(s0.a, 0) AS a0",             # null semantics agree
])
def test_expressions_trino_evaluates_like_duckdb_are_dispatchable(_wide, expr):
    pytest.importorskip("sqlglot")
    assert _athena_reason(_wide, "select", expr) is None


@pytest.mark.parametrize("expr,why", [
    ("s0.a / s0.b AS r",
     "DuckDB `/` is TRUE division (7/2 = 3.5 DOUBLE); Trino's is integer division (3). conform would "
     "cast Trino's 3 to DOUBLE and publish 3.0 — right type, WRONG VALUE"),
    ("CAST(s0.q AS INTEGER) AS a2",
     "at .5 DuckDB and Trino round differently"),
    ("upper(trim(s0.name)) AS n", "trimming and collation are unverified"),
])
def test_expressions_with_a_known_divergence_stay_local(_wide, expr, why):
    pytest.importorskip("sqlglot")
    assert _athena_reason(_wide, "select", expr) is not None, f"{expr} must not dispatch: {why}"


def test_filters_are_judged_too(_wide):
    pytest.importorskip("sqlglot")
    assert _athena_reason(_wide, "filter", "s0.a > 5 AND s0.b IS NOT NULL") is None
    assert _athena_reason(_wide, "filter", "s0.a / 2 > 5") is not None


def test_unparseable_sql_is_never_assumed_safe(_wide):
    pytest.importorskip("sqlglot")
    assert _athena_reason(_wide, "select", "this is not sql (((") is not None
