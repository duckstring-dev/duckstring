"""Translating a Flock job into a SQL engine's dialect, trusted only where measured (plans/flock-coverage.md,
parts 1 to 3).

A job is one DuckDB query (:meth:`trickle.ComprehensiveJob.sql`). For an engine that isn't DuckDB, the query
is parsed with sqlglot, its ``*`` and ``* EXCLUDE`` are expanded against the sources' known columns, every
node is checked against the engine's **profile**, and the tree is generated in the engine's dialect. The
builder's own shapes (every join, filters, computed columns, the aggregate rebuild) are plain SQL, so the
query shape is never the limit; what an engine is trusted with is decided per construct:

- **allowed**: the construct's translation was run on both engines and the results agreed after
  ``conform`` (the engine's conformance test records the evidence);
- **accepted**: a construct with a known, bounded difference (a named *kind*, such as ``float_order``) that
  the Pond has accepted by name (``[flock] accept``);
- anything else keeps the job local, with the construct named in the reason.

A rule of the translation, not of an engine: every value class must survive. A construct that can return a
different value (integer division where DuckDB divides truly, an error where DuckDB returns NULL) is never
allowed or accepted; the acceptance vocabulary only covers differences that are bounded and documented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Query structure: the shapes the builder and ordinary SQL are made of. Their meaning is the same on every
# SQL engine; only the expressions inside them can differ.
STRUCTURAL = frozenset({
    "With", "CTE", "Select", "From", "Join", "Subquery", "TableAlias", "Table", "Column", "Identifier", "Alias",
    "Star", "Where", "Group", "Having", "Paren", "Tuple", "Order", "Ordered", "Distinct", "Window",
    "WindowSpec", "DataType", "DataTypeParam", "Var", "Union", "Exists",
    # values, comparisons and logic
    "Literal", "Boolean", "Null", "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "And", "Or", "Not", "Is", "In",
    "Between", "Neg", "If", "Case", "Coalesce",
})

# What a known, bounded difference is called. A Pond accepts kinds by name; nothing outside this vocabulary
# can be accepted.
ACCEPTANCES = {
    "float_order": "floating-point sums and the statistics built on them (mean, variance, correlation, "
                   "regression) can differ in the last bits, because a parallel engine adds in a different "
                   "order. DuckDB itself isn't bit-stable across thread counts either.",
    "half_rounding": "a CAST to an integer type of a value exactly halfway between two integers rounds "
                     "differently (DuckDB rounds half to even).",
}


@dataclass(frozen=True)
class Profile:
    """What one SQL engine is trusted with, by sqlglot node name (``Sum``, ``Div``, ``Upper``) or, for a
    function sqlglot doesn't model, ``fn:name``.

    - ``allowed``: measured equal on both engines.
    - ``kinds``: construct → the acceptance kind it needs (``{"Sum": "float_order"}`` would mean a Sum is
      allowed only when the Pond accepts ``float_order``). Consulted before ``allowed``'s absence refuses.
    - ``cast_to``: the DuckDB types a CAST may target (as sqlglot ``DataType.Type`` names).
    - ``float_kinds``: constructs that need ``float_order`` only when their input is floating point (a
      DECIMAL or integer SUM is exact everywhere).
    """

    dialect: str
    allowed: frozenset[str]
    kinds: dict[str, str] = field(default_factory=dict)
    cast_to: frozenset[str] = frozenset()
    float_kinds: frozenset[str] = frozenset()
    # Constructs allowed only when every argument is an integer.
    int_only: frozenset[str] = frozenset()
    # ``check(node, exp) -> reason | None``: an engine's own refusals beyond names (an argument type, a form).
    check: object = None
    # Engine-specific rewrites applied after the check, before generation: ``rewrite(tree, exp)`` returns the
    # tree with DuckDB's semantics spelled out where the engine's own differ (division, CAST to an integer).
    rewrite: object = None


@dataclass
class Translation:
    """The job in the engine's dialect, with each source written as ``marker(i)`` (the engine substitutes
    its own read), or the reason it can't be sent."""

    sql: str | None = None
    reason: str | None = None
    leaves: list = field(default_factory=list)


_INT_TYPES = {"TINYINT", "SMALLINT", "INT", "BIGINT", "UTINYINT", "USMALLINT", "UINT", "UBIGINT"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE"}


def marker(i: int) -> str:
    return f"__dsflock_leaf_{i}__"


def _sqlglot():
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.optimizer.annotate_types import annotate_types
        from sqlglot.optimizer.qualify import qualify
    except ImportError as exc:  # pragma: no cover - the extras carry sqlglot
        raise RuntimeError("translating a Flock job needs sqlglot (pip install 'duckstring[starrocks]')") from exc
    return sqlglot, exp, qualify, annotate_types


def translate(job, profile: Profile, accept=(), *, drop_columns=(), check: bool = True) -> Translation:
    """Translate ``job`` for an engine with ``profile``, accepting the named kinds in ``accept``.
    ``drop_columns`` are output columns left out of the engine's query (constants the caller adds back
    locally, such as an aggregate's freshness stamp). ``check=False`` skips the profile (only for measuring
    constructs in the conformance tests)."""
    try:
        sqlglot, exp, qualify, annotate_types = _sqlglot()
    except RuntimeError as exc:
        return Translation(reason=str(exc))
    leaves = job.leaves()
    names = {id(leaf): marker(i) for i, leaf in enumerate(leaves)}
    schema: dict[str, dict[str, str]] = {}
    for leaf in leaves:
        rel = job.ctx.read_table(leaf.ref)
        types = dict(zip(rel.columns, (str(t) for t in rel.types), strict=True))
        schema[names[id(leaf)]] = {c: types[c] for c in job.columns(leaf)}

    def read(leaf) -> str:
        cols = ", ".join('"' + c.replace('"', '""') + '"' for c in job.columns(leaf))
        return f'SELECT {cols} FROM "{names[id(leaf)]}"'

    try:
        duck_sql = job.sql(read)
        tree = sqlglot.parse_one(duck_sql, read="duckdb")
        tree = qualify(tree, schema=schema, dialect="duckdb", expand_stars=True, validate_qualify_columns=False,
                       quote_identifiers=True, identify=True)
        tree = annotate_types(tree, schema=schema, dialect="duckdb")
    except Exception as exc:  # noqa: BLE001 - unparseable or unresolvable means unverifiable
        return Translation(reason=f"could not read the job's SQL ({type(exc).__name__}: {str(exc)[:200]})")

    if drop_columns:
        drop = {c.lower() for c in drop_columns}
        tree.set("expressions", [e for e in tree.expressions if e.alias_or_name.lower() not in drop])
    reason = _check(tree, profile, set(accept), exp, _utc(job.ctx.con)) if check else None
    if reason is not None:
        return Translation(reason=reason)
    for cte in tree.find_all(exp.CTE):
        cte.set("materialized", None)  # NOT MATERIALIZED is DuckDB's; other engines plan CTEs themselves
    _alias_leaves(tree, exp)
    _hoist_ctes(tree, exp)
    if profile.rewrite is not None:
        tree = profile.rewrite(tree, exp)
    try:
        sql = tree.sql(dialect=profile.dialect, identify=True, unsupported_level=sqlglot.ErrorLevel.RAISE)
    except Exception as exc:  # noqa: BLE001
        return Translation(reason=f"no {profile.dialect} translation ({type(exc).__name__}: {str(exc)[:200]})")
    return Translation(sql=sql, leaves=leaves)


def _utc(con) -> bool:
    """Whether DuckDB on the Duck works in UTC: functions over a TIMESTAMPTZ (``date_trunc``, ``hour``) follow
    the session time zone, and a SQL engine works on sources staged as UTC."""
    try:
        tz = con.execute("SELECT current_setting('TimeZone')").fetchone()[0]
    except Exception:  # noqa: BLE001 - no ICU: no time zones, so UTC
        return True
    return str(tz).upper() in ("UTC", "ETC/UTC", "GMT", "ETC/GMT", "Z")


_STRUCT_TZ_OK = ("Column", "Alias", "Paren", "Is", "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Coalesce", "Min",
                 "Max", "Count")


def _check(tree, profile: Profile, accept: set[str], exp, utc: bool = True) -> str | None:
    """``None`` when every construct is structural, allowed, or of an accepted kind; else the reason."""
    for node in tree.walk():
        if not utc and type(node).__name__ not in _STRUCT_TZ_OK and any(
                _type(a) == "TIMESTAMPTZ" for a in (node.args.get("this"), node.args.get("expression"))
                if isinstance(a, exp.Expression)):
            return (f"{_label(node, type(node).__name__)} reads a TIMESTAMPTZ, which depends on the session time "
                    "zone; the Duck isn't in UTC")
        if profile.check is not None:
            reason = profile.check(node, exp)
            if reason:
                return reason
        if isinstance(node, exp.Cast):
            reason = _check_cast(node, profile, accept, exp)
            if reason:
                return reason
            continue
        name = f"fn:{str(node.this).lower()}" if isinstance(node, exp.Anonymous) else type(node).__name__
        kind = profile.kinds.get(name)
        if kind is not None and name in profile.float_kinds and not _is_float(node, exp):
            kind = None  # exact on integer and DECIMAL input
        if kind is not None:
            if kind in accept:
                continue
            return (f"{_label(node, name)} differs on {profile.dialect} in a known way ({kind}); "
                    f'accept it with [flock] accept = ["{kind}"]')
        if name in profile.int_only and not all(
                _type(a) in _INT_TYPES for a in (node.args.get("this"), node.args.get("expression"))
                if isinstance(a, exp.Expression)):
            return f"{_label(node, name)} is measured on integers only on {profile.dialect}"
        if name in STRUCTURAL or name in profile.allowed:
            continue
        return f"{_label(node, name)} hasn't been measured equal on {profile.dialect}"
    return None


def _type(node) -> str:
    t = getattr(node, "type", None)
    return t.this.name if t is not None else "UNKNOWN"


def _check_cast(node, profile: Profile, accept: set[str], exp) -> str | None:
    """A CAST is judged by its target and its source type: a literal to a date or time is exact everywhere; a
    string otherwise may be malformed (DuckDB raises where an engine may return NULL); a floating-point value
    to text is formatted differently by every engine."""
    to = node.to.this.name if isinstance(node.to, exp.DataType) else str(node.to)
    label = f"CAST to {node.to.sql('duckdb')}"
    if isinstance(node.this, exp.Literal) and node.this.is_string and to in ("DATE", "TIMESTAMP", "TIMESTAMPNTZ"):
        return None
    if to not in profile.cast_to:
        return f"{label} hasn't been measured equal on {profile.dialect}"
    source = _type(node.this)
    if source in ("VARCHAR", "TEXT", "CHAR") and to not in ("VARCHAR", "TEXT"):
        return f"{label} of a string isn't sent: DuckDB raises on a malformed value where {profile.dialect} may not"
    if to in ("VARCHAR", "TEXT") and source in _FLOAT_TYPES | {"UNKNOWN"}:
        return f"{label} of a {source} value isn't sent: engines format floating point differently"
    return None


def _is_float(node, exp) -> bool:
    """Whether any argument of ``node`` is (or may be) floating point. Unknown counts as floating."""
    for arg in (node.args.get("this"), node.args.get("expression")):
        if not isinstance(arg, exp.Expression):
            continue
        t = getattr(arg, "type", None)
        if t is None or t.this.name in _FLOAT_TYPES or t.this.name == "UNKNOWN":
            return True
    return False


def _label(node, name: str) -> str:
    if name.startswith("fn:"):
        return f"function {name[3:]}()"
    sql = node.sql("duckdb")
    return f"{name} ({sql[:60]}{'…' if len(sql) > 60 else ''})"


def _alias_leaves(tree, exp) -> None:
    """Give each source read its own alias (``dsl{i}``) and point column qualifiers at it, so the marker only
    names the table, which the engine replaces with its own read."""
    for table in list(tree.find_all(exp.Table)):
        m = _MARKER_RE.fullmatch(table.name)
        if m is None:
            continue
        old, new = table.alias_or_name, f"dsl{m.group(1)}"
        table.set("alias", exp.TableAlias(this=exp.to_identifier(new, quoted=True)))
        scope = table.parent_select
        if scope is None:
            continue
        for col in scope.find_all(exp.Column):
            if col.table == old:
                col.set("table", exp.to_identifier(new, quoted=True))


def _hoist_ctes(tree, exp) -> None:
    """Move a CTE list nested inside another CTE's body up to the top level when the names don't collide
    (some engines refuse a WITH inside a CTE). The builder's CTE names are unique by construction."""
    top = tree.args.get("with_") or tree.args.get("with")
    if top is None:
        return
    names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    if len(names) != len(list(tree.find_all(exp.CTE))):
        return
    hoisted = []
    for cte in list(top.expressions):
        inner = cte.this
        nested = inner.args.get("with_") or inner.args.get("with")
        if nested is not None:
            inner.set("with_" if "with_" in inner.args else "with", None)
            hoisted.extend(nested.expressions)
        hoisted.append(cte)
    top.set("expressions", hoisted)


_MARKER_RE = re.compile(r"[`\"]?__dsflock_leaf_(\d+)__[`\"]?")


def substitute(sql: str, read) -> str:
    """Replace each source marker in a generated query with ``read(i)``, the engine's own read of source
    ``i`` (a table name, or a table function such as StarRocks' ``FILES(...)``)."""
    return _MARKER_RE.sub(lambda m: read(int(m.group(1))), sql)
