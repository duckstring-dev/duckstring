"""The ``pond.trickle(...)`` builder — a DBSP-style **DAG of binary incremental joins** over Z-set sources.

A fluent builder that records an **operator DAG** (sources + binary equi-joins of any ``how``, plus an
ordered ``filter``/``mutate``/``select`` output pipeline) and maintains its output incrementally. Each join
node's delta is a sum of terms over the current states ``A'``, ``B'`` and the deltas ``δA``, ``δB``
(``plans/trickle-join-delta.md``), never a prior state:

    δ(A ⋈ B) = δA ⋈ B'  +  A' ⋈ δB  −  δA ⋈ δB          -- inner: a row's weight is its operands' product

The existence joins and the outer joins' NULL-padded rows depend on whether *any* ``B`` row has a key, which
only changes for the **flip set** ``F`` (keys of ``δB`` whose match count crossed zero, sign ``s``):

    δ(A ⋉ B) = δA ⋉ B'  +  s·A'|F  −  s·δA|F                 δ(A ▷ B) = δA ▷ B'  −  s·A'|F  +  s·δA|F

``left`` is the inner terms plus the padded ``δ(A ▷ B)``, ``right`` its mirror, ``full`` both. Each term joins
a small side (a delta or ``F``) to a current state, so the join itself restricts the large side; for a
current state that is itself a join, the small side's keys are also pushed down to the leaf that owns them,
tightening any outer join on the way that would pad that leaf's side (``key_filter``). The root's delta is
consolidated once, before the output pipeline. No privileged spine, so a bushy ``(A⋈B)⋈(C⋈D)`` and outer joins
compose freely.

Because deletions are full-row ``−1`` tuples (not key tombstones), a join may be on **any** key. The DAG
composes to **inline SQL** (the planner's gate, ``plans/trickle-dag.md``: per-node materialisation loses to
inline nested views at every scale) — the only persistence fence is an explicit ``.merge()`` boundary, which
also buys cross-run reuse of the materialised intermediate.

When any source can't supply a clean delta — a **bootstrap**, a **coverage-miss**, a **changed overwrite
Ripple**, or a delta over its change-fraction threshold ``p`` — that subtree (and so the whole output) is
recomputed and diffed against the **materialised prior output** (the last-written *main*, read not
recomputed). An *unchanged* overwrite Ripple is a free stable operand.
"""

from __future__ import annotations

import re

from .context import SYSTEM_PREFIX
from .io import (
    D_COL,
    F_COL,
    RESCAN_KINDS,
    _q,
    _table_exists,
    _ts,
    first_row,
    normalize_pk,
    read_meta,
    read_registry_delta,
    unique_name,
)


class BuildError(ValueError):
    """The Trickle builder was used incorrectly: a missing ``pk``, an ambiguous join key, or a method used
    where it isn't allowed. A subclass of ``ValueError``.
    """


# A verbatim source pass-through select item: ``<alias>.col`` / ``<alias>."col"`` / ``<alias>.col AS x``.
# group(2) = source column, group(4) = output alias (if any). Built per spine alias for the fast-path detect.
def _passthrough_re(alias: str):
    return re.compile(rf'^{re.escape(alias)}\.("?)(\w+)\1(?:\s+as\s+("?)(\w+)\3)?$', re.IGNORECASE)


# DuckDB type → Ibis type-string (for ``to_ibis_schema``; no ibis dependency — a plain dict ``ibis.table``
# accepts). Best-effort over the common types; an unmapped type raises rather than guessing.
_DUCKDB_TO_IBIS = {
    "BOOLEAN": "boolean", "BOOL": "boolean",
    "TINYINT": "int8", "SMALLINT": "int16", "INTEGER": "int32", "INT": "int32", "BIGINT": "int64",
    "HUGEINT": "int128", "UTINYINT": "uint8", "USMALLINT": "uint16", "UINTEGER": "uint32", "UBIGINT": "uint64",
    "FLOAT": "float32", "REAL": "float32", "DOUBLE": "float64",
    "VARCHAR": "string", "TEXT": "string", "CHAR": "string", "BLOB": "binary",
    "DATE": "date", "TIME": "time", "TIMESTAMP": "timestamp", "DATETIME": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamp('UTC')", "TIMESTAMPTZ": "timestamp('UTC')",
    "UUID": "uuid", "INTERVAL": "interval",
}


def _duckdb_to_ibis(t: str) -> str:
    up = t.strip().upper()
    if up in _DUCKDB_TO_IBIS:
        return _DUCKDB_TO_IBIS[up]
    if up.startswith("DECIMAL"):  # DECIMAL(p,s) → decimal(p,s)
        return "decimal" + t.strip()[7:]
    raise BuildError(f"to_ibis_schema(): no Ibis mapping for DuckDB type {t!r} — use .schema() and map it yourself")


# Supported join types → their DuckDB keyword. All six are now maintained incrementally (per-node
# affected-key recompute), including the outer incomparables, so any of them can sit anywhere in a DAG.
_JOIN_SQL = {"inner": "JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL JOIN",
             "semi": "SEMI JOIN", "anti": "ANTI JOIN"}
# Join types whose output is the left side only (existence filters) — no right-side columns in the output.
_LEFT_ONLY = {"semi", "anti"}
# Join types whose output includes the matched (inner) rows.
_MATCHED = {"inner", "left", "right", "full"}
# The joins whose delta is one pass from a changed left (right) side when the other side is unchanged: a single
# term reads that delta once (TrickleBuilder._one_pass, _matched_terms, _exists_delta).
_ONE_PASS_LEFT = {"inner", "left", "semi", "anti"}
_ONE_PASS_RIGHT = {"inner", "right"}
# A join a keys filter passes down through (``_pushed``). Whatever uses the filtered subtree joins it on the key
# by equality, so a row whose key side is NULL-padded never contributes. Filtering a padded side would turn rows
# it removes into such padded rows (harmless, but as many as the other side has rows), so the join is tightened
# to drop them instead: ``_TIGHTEN[(how, side)]`` is the join without padding on ``side``. The right side of a
# semi or anti join never reaches the output, so a key there can't be pushed.
_TIGHTEN = {
    ("inner", "left"): "inner", ("inner", "right"): "inner",
    ("left", "left"): "left", ("left", "right"): "inner",
    ("right", "left"): "inner", ("right", "right"): "right",
    ("full", "left"): "left", ("full", "right"): "right",
    ("semi", "left"): "semi", ("anti", "left"): "anti",
}


def _cols(on) -> tuple[str, ...]:
    return (on,) if isinstance(on, str) else tuple(on)


def _join_pairs(on) -> list[tuple[str, str]]:
    """Normalise ``on`` to ``[(left_name, right_name), …]``. A str/list names columns shared by both sides;
    a dict maps left columns to right columns (when the names differ). A name may be ``alias.col`` to
    disambiguate when several sources share a bare column name."""
    if isinstance(on, dict):
        return [(s, d) for s, d in on.items()]
    return [(c, c) for c in _cols(on)]


def _select_items(projection: str) -> list[str]:
    """Split a SQL select list on **top-level** commas (ignoring those inside parens or quotes), so a
    computed item like ``round(a, 2) AS x`` stays one piece."""
    items, depth, buf, quote = [], 0, [], None
    for ch in projection:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        items.append("".join(buf).strip())
    return items


_IDENT = re.compile(r"[A-Za-z_]\w*")
_COLPART = re.compile(r'"[^"]*"|[A-Za-z_]\w*')


def _qualify(text: str, aliases: set[str]) -> str:
    """Rewrite leaf references ``alias.col`` / ``alias."col"`` to the internal qualified column name
    ``"alias.col"`` (a single dotted, quoted identifier) — for any ``alias`` in ``aliases``. String literals
    and unknown identifiers are left untouched; ``alias.*`` is handled by the caller (projection star
    expansion), not here."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":  # a string literal — copy verbatim (incl. doubled '' escapes)
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        m = _IDENT.match(text, i)
        if m:
            word = m.group(0)
            k = m.end()
            if word in aliases and k < n and text[k] == ".":
                cm = _COLPART.match(text, k + 1)
                if cm:
                    col = cm.group(0)
                    bare = col[1:-1] if col.startswith('"') else col
                    out.append(_q(f"{word}.{bare}"))
                    i = cm.end()
                    continue
            out.append(word)
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ─── operator DAG nodes ────────────────────────────────────────────────────────


class _Source:
    """A leaf: a base table read as a Z-set source. ``alias`` is the explicit ``.alias()`` name (else a
    positional ``s{i}`` is assigned at compile); ``threaded_delta`` is set when this source is a
    just-materialised in-run Trickle (a chained ``.merge()`` result read from the registry, not the
    not-yet-published data plane). It may be given as a function, called the first time it's used, so a
    terminal whose result nothing chains from never reads its own change back."""

    def __init__(self, ref: str, p: float, *, alias: str | None = None, threaded_delta=None) -> None:
        self.ref = ref
        self.p = p
        self.alias = alias
        self._threaded = threaded_delta

    @property
    def threaded_delta(self):
        if callable(self._threaded):
            self._threaded = self._threaded()
        return self._threaded

    def leaves(self) -> list["_Source"]:
        return [self]


class _Join:
    """An internal node: a binary equi-join ``left ⋈ right`` of type ``how`` on ``on_pairs`` (raw column
    names, resolved to qualified columns at compile)."""

    def __init__(self, left, right, on_pairs, how: str) -> None:
        self.left = left
        self.right = right
        self.on_pairs = on_pairs
        self.how = how

    def leaves(self) -> list["_Source"]:
        return self.left.leaves() + self.right.leaves()


class _NodeState:
    """The compiled SQL handles for one DAG node over this run: ``cols`` (qualified output columns),
    ``current`` (view name for the full current state), ``delta`` (view name for the Z-set ΔO, unconsolidated
    for a join, or ``None``), ``changed`` and ``is_full``. ``current`` may be given as a function, built the
    first time a term reads it: a Source's current state can be costly to read (a merge Trickle's change log
    is consolidated), and a run whose terms only need the deltas never reads it. ``insert_only``: the delta
    holds only added rows (see :meth:`TrickleBuilder._compile_join` for when a join's does), so it needs no
    consolidation."""

    __slots__ = ("cols", "_current", "delta", "changed", "is_full", "insert_only")

    def __init__(self, cols, current, delta, changed, is_full, insert_only=False) -> None:
        self.cols = cols
        self._current = current
        self.delta = delta
        self.changed = changed
        self.is_full = is_full
        self.insert_only = insert_only and delta is not None

    @property
    def current(self) -> str:
        if callable(self._current):
            self._current = self._current()
        return self._current


class TrickleBuilder:
    """A Trickle builder, started with ``pond.trickle(ref)``.

    Chain methods to compose joins, filters, computed columns, aggregations and accumulations, then call a
    terminal (:meth:`merge` or :meth:`append`) to compute and write the result. ``.filter``, ``.mutate``
    and ``.select`` apply in call order. Sources are named ``s0``, ``s1``, ... left to right unless given an
    :meth:`alias`. Without a ``.select``, the output is every column, with join keys appearing once.

    Reference: https://docs.duckstring.com/reference/python/trickle_builder
    """

    def __init__(self, ctx, spine_ref: str, *, p: float = 0.3, _spine_delta=None) -> None:
        self.ctx = ctx
        self.spine_ref = spine_ref
        self.p = p
        self._root = _Source(spine_ref, p, threaded_delta=_spine_delta)
        # Whether the .merge()/.append() that produced THIS (chained) handle actually changed its output —
        # the pond.skip() signal, read via .was_changed(). True for a fresh source builder (unwritten).
        self._was_changed = True
        # The output pipeline: an ordered list of ("filter", pred) / ("mutate", {name: expr}) / ("select",
        # projection) stages, applied (in call order) to the composed DAG result. Each is a row-local,
        # deterministic map, so the whole pipeline distributes over the Z-set delta — incrementally free.
        self._ops: list[tuple[str, object]] = []
        self._alias: str | None = None  # this node's alias (the spine's, for .select refs / the .sql table)
        # After .sql(): ``(parent, query, alias)``, the deferred query over the parent's full result, run at the
        # terminal (locally, or offered to the host first). ``_materialised`` caches its result relation.
        self._sql_of = None
        self._materialised = None
        self._agg = None  # {"by": (...), "metrics": {out: agg.Metric}} after .aggregate()
        self._agg_by: tuple[str, ...] | None = None
        self._along: str | None = None  # the monotonic order axis for .accumulate() (set by .along())
        self._acc = None  # {"by": (...), "metrics": {out: acc.AccMetric}} after .accumulate()
        self._key_filter = True  # set per-terminal from the .merge()/.append() key_filter flag
        # compile-scoped caches (rebuilt per terminal): leaf → alias, leaf → bare cols, node → compiled state
        self._alias_of: dict[int, str] = {}
        self._cols_cache: dict[int, list[str]] = {}
        self._states: dict[int, _NodeState] = {}
        self._scratch: list[str] = []  # temp tables a compile made (materialised join deltas), dropped by _chain
        self._pushed_memo: dict = {}  # _pushed's views by (node, key columns, keys view): reused, then materialised
        # While emitting a comprehensive recompute as one query (:meth:`_query_sql`), every view the compile
        # creates is also logged here as ``(name, sql, leaf)``; ``leaf`` is set for a source's base read.
        self._view_log: list | None = None

    # ─── fluent surface ─────────────────────────────────────────────────────────

    def alias(self, name: str) -> "TrickleBuilder":
        """Name this source so expressions refer to it as ``name.col`` instead of ``s0.col``. Aliases must be
        unique within a builder. :meth:`sql` needs one, as the table name its query selects from.
        """
        self._alias = name
        if isinstance(self._root, _Source):
            self._root.alias = name
        return self

    def join(self, dimension: "TrickleBuilder", *, on, how: str = "inner") -> "TrickleBuilder":
        """Join another builder on an equality condition.

        Args:
            dimension: The right-hand side: ``pond.trickle(...)``, or itself a chain of joins.
            on: A column name on both sides, a list of them, or a ``{left: right}`` mapping. A name can be
                qualified as ``alias.col`` to pick a side.
            how: ``"inner"`` (default), ``"left"``, ``"right"``, ``"full"``, ``"semi"`` or ``"anti"``. All are
                maintained incrementally; ``semi`` and ``anti`` keep only left-side columns.

        The right-hand side can't carry its own ``.filter()``, ``.mutate()``, ``.select()``, ``.aggregate()``
        or ``.sql()``; apply those to the joined result.
        """
        self._ensure_incremental("join")
        how = how.lower()
        if how not in _JOIN_SQL:
            raise BuildError(f"join(how={how!r}): one of {sorted(_JOIN_SQL)}")
        if not isinstance(dimension, TrickleBuilder):
            raise BuildError("join() takes another pond.trickle(...) operand")
        if dimension._is_sql or dimension._agg is not None:
            raise BuildError("join(): a .sql()/.aggregate() result can't be a join operand — do it downstream")
        if dimension._ops:
            raise BuildError(
                f"join('{dimension.spine_ref}'): a join operand can't carry its own .filter()/.mutate()/"
                f".select() — attach those to the composed result, or materialise it with a downstream .merge()"
            )
        self._root = _Join(self._root, dimension._root, _join_pairs(on), how)
        return self

    def filter(self, predicate: str) -> "TrickleBuilder":
        """Keep rows matching a SQL boolean ``predicate``, over the source columns (``s0.col`` or ``alias.col``)
        and any column added by an earlier :meth:`mutate`.
        """
        self._ensure_incremental("filter")
        self._ops.append(("filter", predicate))
        return self

    def mutate(self, **columns: str) -> "TrickleBuilder":
        """Add computed columns, keeping the existing ones. Each keyword is a column name and a SQL expression.

        A name matching an existing column replaces it. Columns in one call can't refer to each other; chain
        another call to build on a new column. A computed column can be the output ``pk`` but not a join key.
        Expressions must be deterministic (no ``now()`` or ``random()``), since removals cancel against earlier
        additions by matching whole rows.
        """
        self._ensure_incremental("mutate")
        if not columns:
            raise BuildError("mutate() needs at least one name=expr column")
        for name in columns:
            if name.startswith(SYSTEM_PREFIX):
                raise BuildError(f"mutate(): column '{name}' uses the reserved '{SYSTEM_PREFIX}' prefix")
        self._ops.append(("mutate", dict(columns)))
        return self

    def select(self, projection: str) -> "TrickleBuilder":
        """Set the output columns with a SQL select list, replacing the current set. Items may be computed
        (``expr AS name``). Must include the output ``pk``. Needed when column names clash across joined sources.
        """
        self._ensure_incremental("select")
        self._ops.append(("select", projection))
        return self

    # Compat views over the pipeline for the guards/fast-paths that predate it.
    @property
    def _filters(self) -> list[str]:
        return [p for kind, p in self._ops if kind == "filter"]

    @property
    def _projection(self) -> str | None:
        sels = [p for kind, p in self._ops if kind == "select"]
        return sels[-1] if sels else None

    @property
    def _has_mutate(self) -> bool:
        return any(kind == "mutate" for kind, _ in self._ops)

    @property
    def _is_sql(self) -> bool:
        """Whether this builder is a :meth:`sql` result (comprehensive mode)."""
        return self._sql_of is not None or self._materialised is not None

    def _ensure_incremental(self, op: str) -> None:
        if self._is_sql:
            raise BuildError(
                f".{op}() isn't available after .sql() (the result is materialised, no longer a Z-set) — "
                f"compose joins/filters/projection before .sql(), or chain another .sql()"
            )
        if self._agg is not None:
            raise BuildError(
                f".{op}() can't follow .aggregate() — aggregate is terminal-bound to .merge(); do further "
                f"work in a downstream Trickle"
            )
        if self._acc is not None:
            raise BuildError(
                f".{op}() can't follow .accumulate() — finish the scan with .append(); do further work in a "
                f"downstream Trickle"
            )

    def along(self, col: str) -> "TrickleBuilder":
        """Set the column that orders rows within each group for :meth:`accumulate` and ``agg.reduce``. For
        ``.accumulate(...).append()``, it must never decrease from one run to the next.
        """
        self._ensure_incremental("along")
        self._along = col
        return self

    def accumulate(self, by=None, **metrics) -> "TrickleBuilder":
        """Add a running value to every row, in :meth:`along` order within each ``by`` group.

        Args:
            by: Grouping column or columns. Omit for a single group.
            **metrics: Output column name to an ``acc`` spec, e.g. ``running=acc.sum("qty")``.

        Finish with :meth:`append` (rows only added, in increasing order; cost proportional to new rows) or
        :meth:`merge` (any change, including removals and late rows; a changed group is recomputed).
        Requires :meth:`along` first.
        """
        self._ensure_incremental("accumulate")
        from .acc import AccMetric

        if self._along is None:
            raise BuildError(".accumulate() needs an order axis — call .along('col') first")
        if not metrics:
            raise BuildError(".accumulate() needs ≥1 metric, e.g. total=acc.sum('qty')")
        spec = {}
        for out, m in metrics.items():
            if not isinstance(m, AccMetric):
                raise BuildError(f"accumulate metric '{out}' must be an acc.* spec (acc.sum/count/min/max/ema/tema)")
            spec[out] = m
        self._acc = {"by": normalize_pk(by) if by else (), "metrics": spec}
        return self

    def group_by(self, by) -> "TrickleBuilder":
        """Set the grouping columns for a following :meth:`aggregate`: ``.group_by(by).aggregate(**metrics)``."""
        self._ensure_incremental("group_by")
        self._agg_by = normalize_pk(by)
        return self

    def aggregate(self, by=None, **metrics) -> "TrickleBuilder":
        """Group the result by ``by`` and maintain one row per group.

        Args:
            by: Grouping column or columns. They become the output's primary key.
            **metrics: Output column name to an ``agg`` spec, e.g. ``total=agg.sum("revenue")``.

        Must be finished with :meth:`merge`, whose ``pk`` defaults to ``by``. ``agg.reduce`` needs
        :meth:`along` first and can't share the call with other metrics.
        """
        self._ensure_incremental("aggregate")
        from .agg import Metric

        if self._agg is not None:
            raise BuildError("one .aggregate() per builder")
        by = normalize_pk(self._agg_by if by is None else by)
        if not by:
            raise BuildError(".aggregate() needs a group key — .aggregate(by=…) or .group_by(…).aggregate(…)")
        if not metrics:
            raise BuildError(".aggregate() needs ≥1 metric, e.g. total=agg.sum('revenue')")
        spec = {}
        for out, m in metrics.items():
            if not isinstance(m, Metric):
                raise BuildError(f"aggregate metric '{out}' must be an agg.* spec (e.g. agg.sum/mean/var/covariance)")
            spec[out] = m
        if any(m.kind == "reduce" for m in spec.values()):
            if self._along is None:
                raise BuildError("agg.reduce(...) is order-dependent — call .along('col') before .aggregate()")
            if not all(m.kind == "reduce" for m in spec.values()):
                raise BuildError("agg.reduce(...) can't share an .aggregate() with other metrics — split them")
        self._agg = {"by": by, "metrics": spec}
        return self

    def sql(self, query) -> "TrickleBuilder":
        """Run any SQL over the result so far, for operations the builder doesn't maintain incrementally (window
        functions, ``DISTINCT``, medians, set operations).

        The result so far is computed in full and exposed under this builder's :meth:`alias`; ``query`` is a
        SQL string selecting from it, or an Ibis expression (compiled lazily; needs ``ibis-framework``). The
        returned builder is recomputed in full each run, but its terminal still writes only changed rows.
        The query runs when the terminal does. Afterwards only ``.alias()``, ``.sql()``, ``.schema()``, ``.count()``
        and the terminals are available.
        """
        if self._agg is not None:
            raise BuildError(".sql() can't follow .aggregate() — aggregate is terminal-bound to .merge()")
        if self._alias is None:
            raise BuildError(".sql() needs a table name to reference — call .alias('t') first, then '… FROM t'")
        if not isinstance(query, str):  # an Ibis expression → compile to DuckDB SQL (ibis only imported here)
            import ibis

            query = str(ibis.to_sql(query, dialect="duckdb"))
        out = TrickleBuilder(self.ctx, unique_name("sqlout"))
        out._sql_of = (self, query.strip().rstrip(";").strip(), self._alias)
        return out

    def _materialise(self):
        """Run a :meth:`sql` builder's query locally (once) and return its result relation: the parent's full
        result is exposed under the alias, and the query lands in a temp table."""
        if self._materialised is None:
            parent, query, alias = self._sql_of
            # A joined DAG with no .select() resolves via the bare `*` (join keys deduplicated); a genuine name
            # collision raises a precise BuildError from _star_output when _full_join projects.
            base = parent._materialise() if parent._is_sql else parent._full_join()
            base.create_view(alias, replace=True)
            self.ctx.con.execute(f'CREATE OR REPLACE TEMP TABLE {_q(self.spine_ref)} AS {query}')
            self._materialised = self.ctx.con.sql(f'SELECT * FROM {_q(self.spine_ref)}')
        return self._materialised

    def _bound(self):
        """This builder's full result as a relation that is bound but not run (for its schema). A :meth:`sql`
        builder binds its query over the parent's bound result, so nothing executes."""
        if not self._is_sql:
            return self._full_join()
        if self._materialised is not None:
            return self._materialised
        parent, query, alias = self._sql_of
        parent._bound().create_view(alias, replace=True)
        return self.ctx.con.sql(query)

    def schema(self) -> dict[str, str]:
        """The current output's columns and DuckDB types, without writing anything."""
        rel = self._bound()
        return {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}

    def to_ibis_schema(self) -> dict[str, str]:
        """:meth:`schema` as Ibis type strings, for ``ibis.table(...)``. Raises for a type with no Ibis equivalent."""
        return {c: _duckdb_to_ibis(t) for c, t in self.schema().items()}

    # ─── terminals ──────────────────────────────────────────────────────────────

    def merge(self, name: str, *, pk=None, ivm: bool = True, key_filter: bool = True,
              retain_t=None, retain_n=None, cluster_by=None, interleave: bool = True,
              cluster_bits=None) -> "TrickleBuilder":
        """Compute the result and write its changes to the merge Trickle ``name``.

        Args:
            name: Output table.
            pk: The output's primary key, which must be unique. Defaults to the ``by`` columns after
                :meth:`aggregate`; otherwise required.
            ivm: ``False`` ignores source changes and recomputes the whole output each run.
            key_filter: ``False`` stops changed keys being passed down into an operand that is itself a join.
                No effect with ``ivm=False``.
            retain_t, retain_n: Change-log retention, as for ``pond.merge_table``.
            cluster_by, interleave, cluster_bits: How the table's compacted base is ordered, as for
                ``pond.merge_table``.

        Only change ``ivm`` or ``key_filter`` after measuring that the default is slower. Returns a builder
        rooted at the written table, so the chain can continue; see :meth:`was_changed`.
        """
        from .io import _set_cluster, cluster_spec

        cluster = cluster_spec(cluster_by, interleave, cluster_bits)  # validated before anything runs
        out = self._merge(name, pk=pk, ivm=ivm, key_filter=key_filter, retain_t=retain_t, retain_n=retain_n)
        _set_cluster(self.ctx.con, name, cluster)  # every write path above; the default clears it
        return out

    def _merge(self, name: str, *, pk, ivm: bool, key_filter: bool, retain_t, retain_n) -> "TrickleBuilder":
        ctx = self.ctx
        if self._acc is not None:
            return self._merge_accumulate(name, pk=pk, retain_t=retain_t, retain_n=retain_n)
        if self._agg is not None:
            from . import io as trickle

            by, metrics = self._agg["by"], self._agg["metrics"]
            if any(m.kind == "reduce" for m in metrics.values()):   # order-dependent custom reduction
                kind, rel = self._compute((*by, self._along), name, ivm=ivm, key_filter=key_filter)
                trickle.apply_ordered_reduce(ctx.con, name, by, self._along, metrics, kind, rel,
                                             self._full_join(), ctx.f, retain_t=retain_t, retain_n=retain_n)
                return self._chain(name, by)
            out_pk = normalize_pk(pk) if pk is not None else by
            required = tuple(dict.fromkeys(
                by + tuple(c for m in metrics.values() for c in (m.col, m.col2) if c is not None)))
            kind, rel = self._compute(required, name, ivm=ivm, key_filter=key_filter, offer="aggregate")
            needs_current = kind == "incremental" and any(m.kind in RESCAN_KINDS for m in metrics.values())
            current = self._full_join if needs_current else None  # built only if a group must rescan
            trickle.apply_aggregate(ctx.con, name, by, metrics, kind, rel, current, ctx.f,
                                    retain_t=retain_t, retain_n=retain_n)
            return self._chain(name, out_pk)
        out_pk = normalize_pk(pk)
        if not out_pk:
            raise BuildError(f"pond.trickle('{self.spine_ref}')...merge('{name}'): pass the output key, merge(pk=...)")
        if self._is_sql:  # comprehensive mode (post-.sql) → diff the relation vs prior main
            changed = ctx.merge_table(name, self._sql_result(out_pk), pk=out_pk, retain_t=retain_t, retain_n=retain_n)
            return self._chain(name, out_pk, changed=changed)
        kind, rel = self._compute(out_pk, name, ivm=ivm, key_filter=key_filter, offer="rows", consolidate=False)
        if kind == "comprehensive":
            changed = ctx.merge_table(name, rel, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
        elif kind == "incremental":
            extra = {"insert_only": True} if self._insert_only else {}  # nothing to consolidate before writing
            changed = ctx.apply_zset(name, rel, pk=out_pk, retain_t=retain_t, retain_n=retain_n, **extra)
        else:
            changed = False  # "empty": nothing changed (and no full read) → output unchanged, no write.
        return self._chain(name, out_pk, changed=changed)

    def append(
        self, name: str, *, pk=None, fail_on_conflict=True, log_drops=True, ivm: bool = True,
        key_filter: bool = True, retain_t=None, retain_n=None
    ) -> "TrickleBuilder":
        """Compute the result and add its new rows to the append Trickle ``name``.

        For transformations whose output rows are only ever added. A removed output row, or an added row whose
        ``pk`` is already present with different values, is a conflict; an identical row is skipped.

        Args:
            name: Output table.
            pk: The output's primary key, needed to detect changed rows.
            fail_on_conflict: Raise ``DeltaError`` on a conflict, before writing. ``False`` drops conflicting
                rows and keeps history as it was.
            log_drops: With ``fail_on_conflict=False``, record dropped rows in a published ``{name}__droplog``.
            ivm, key_filter, retain_t, retain_n: As for :meth:`merge`.

        When ``pk`` is the first source's own key passed through as ``s0.col`` and both ``fail_on_conflict``
        and ``log_drops`` are ``False``, only that source's new rows are joined, which is much cheaper when
        other sources change a lot. Returns a builder rooted at the written table.
        """
        ctx = self.ctx
        if self._acc is not None:
            return self._append_accumulate(name, pk=pk, fail_on_conflict=fail_on_conflict, log_drops=log_drops,
                                            retain_t=retain_t, retain_n=retain_n)
        if self._agg is not None:
            raise BuildError(".append() can't follow .aggregate() — an aggregate updates groups; use .merge()")
        out_pk = normalize_pk(pk)
        from . import io as trickle

        if self._is_sql:  # comprehensive mode (post-.sql) → +1 the relation, append-filter
            changed = trickle.append_zset(
                ctx.con, name, trickle._as_zset(self._sql_result(out_pk), 1), ctx.f, out_pk,
                fail_on_conflict=fail_on_conflict, log_drops=log_drops, retain_t=retain_t, retain_n=retain_n,
            )
            return self._chain(name, out_pk, changed=changed)

        spine_delta = self._spine_delta_value()
        if (ivm and isinstance(self._root, _Join) and not fail_on_conflict and not log_drops
                and self._spine_pk_passthrough(out_pk, spine_delta.pk)):
            candidate = self._full_join(spine_rel=self._new_spine_rows(spine_delta, name, out_pk))
            changed = trickle.append_zset(
                ctx.con, name, trickle._as_zset(candidate, 1), ctx.f, out_pk,
                fail_on_conflict=False, log_drops=False, retain_t=retain_t, retain_n=retain_n,
            )
            return self._chain(name, out_pk, changed=changed)

        kind, rel = self._compute(out_pk, name, ivm=ivm, key_filter=key_filter, offer="rows", consolidate=False)
        changed = False
        if kind != "empty":
            zset = trickle._as_zset(rel, 1) if kind == "comprehensive" else rel
            changed = trickle.append_zset(
                ctx.con, name, zset, ctx.f, out_pk,
                fail_on_conflict=fail_on_conflict, log_drops=log_drops, retain_t=retain_t, retain_n=retain_n,
            )
        return self._chain(name, out_pk, changed=changed)

    def _acc_required(self, out_pk):
        """The columns the scan needs present in the composed rows: ``by`` + ``along`` + each metric's input
        column + the output PK. (A custom ``acc.scan`` reads the whole row, so its columns must be in the
        composed output too — that's the user's responsibility via ``.select`` on a joined build.)"""
        by, metrics, along = self._acc["by"], self._acc["metrics"], self._along
        return tuple(dict.fromkeys((*by, along, *out_pk) + tuple(m.col for m in metrics.values() if m.col)))

    def _append_accumulate(self, name, *, pk, fail_on_conflict, log_drops, retain_t, retain_n):
        """The ``.accumulate(...).append(...)`` path: compose ΔO, then fold the per-row scan on from each
        group's carried state and append the enriched rows (:func:`duckstring.trickle.io.apply_accumulate`)."""
        from . import io as trickle

        by, metrics, along = self._acc["by"], self._acc["metrics"], self._along
        out_pk = normalize_pk(pk)
        kind, rel = self._compute(self._acc_required(out_pk), name, ivm=True, key_filter=True, consolidate="always")
        trickle.apply_accumulate(
            self.ctx.con, name, by, along, metrics, kind, rel, self.ctx.f, pk,
            fail_on_conflict=fail_on_conflict, log_drops=log_drops, retain_t=retain_t, retain_n=retain_n,
        )
        return self._chain(name, out_pk)

    def _merge_accumulate(self, name, *, pk, retain_t, retain_n):
        """The ``.accumulate(...).merge(...)`` path: a **retraction-aware** ordered scan. Compose ΔO (which may
        carry retractions / out-of-order edits) and re-fold the affected groups over their current membership,
        diffing against the prior main (:func:`duckstring.trickle.io.apply_accumulate_merge`)."""
        from . import io as trickle

        by, metrics, along = self._acc["by"], self._acc["metrics"], self._along
        out_pk = normalize_pk(pk)
        if not out_pk:
            raise BuildError(f"pond.trickle('{self.spine_ref}')...accumulate(...).merge('{name}'): pass merge(pk=...)")
        kind, rel = self._compute(self._acc_required(out_pk), name, ivm=True, key_filter=True, consolidate="always")
        trickle.apply_accumulate_merge(
            self.ctx.con, name, by, along, metrics, kind, rel, self._full_join(), self.ctx.f, pk,
            retain_t=retain_t, retain_n=retain_n,
        )
        return self._chain(name, out_pk)

    def count(self) -> int:
        """The number of rows this builder represents, computed now. A bare source or just-written table is
        counted from metadata; a composed builder is computed in full and counted; after :meth:`aggregate`, the
        number of groups is returned without computing the metrics.
        """
        from . import io as trickle

        con = self.ctx.con
        bare = (isinstance(self._root, _Source) and not self._ops
                and not self._is_sql and self._agg is None)
        if bare:
            return self._source_rows(self._root.ref)
        view = trickle.unique_name("count")
        if self._agg is not None:
            # group count: distinct groups in the current composed state — never runs the metric aggregations.
            by = ", ".join(_q(c) for c in self._agg["by"])
            self._full_join().create_view(view, replace=True)
            return int(first_row(con.execute(f'SELECT count(*) FROM (SELECT DISTINCT {by} FROM {_q(view)})'))[0])
        rel = self._materialise() if self._is_sql else self._full_join()
        rel.create_view(view, replace=True)
        return int(first_row(con.execute(f'SELECT count(*) FROM {_q(view)}'))[0])

    # ─── the append spine-PK fast path (output keyed by the spine's own identity) ──

    def _spine_pk_passthrough(self, out_pk, spine_pk):
        """If every output-PK column is a verbatim ``s0.<col>`` pass-through of the **spine's** PK, return
        ``{out_col: spine_col}``; else ``None``. Conservative — it bails on any computed / non-``s0`` /
        ambiguous PK column, so a false positive (which would wrongly drop rows) is impossible."""
        spine_pk = tuple(spine_pk or ())
        if not spine_pk or not out_pk or self._projection is None:
            return None
        pat = _passthrough_re(self._alias or "s0")
        proj = {}
        for item in _select_items(self._projection):
            m = pat.match(item)
            if m:
                proj[(m.group(4) or m.group(2))] = m.group(2)
        smap = {}
        for p in out_pk:
            if p not in proj:
                return None
            smap[p] = proj[p]
        return smap if set(smap.values()) == set(spine_pk) else None

    def _new_spine_rows(self, spine_delta, name: str, out_pk):
        """The spine rows that arrived/changed this run whose (mapped) PK is **not yet** in the output
        history — the only rows that can produce a new append row when the output is spine-PK keyed.

        The history prefilter **excludes rows stamped at the current run's ``f``** (this attempt's own
        output on a same-``f`` replay): ``append_zset``'s replay ``DELETE(@f)`` then re-inserts the epoch's
        rows, so counting them as history would drop them (silent data loss on any retried/replayed run —
        first runs are unaffected because no ``f``-stamped output exists yet)."""
        con = self.ctx.con
        new = spine_delta.upserts
        smap = self._spine_pk_passthrough(out_pk, spine_delta.pk)
        if not _table_exists(con, name):
            return new
        v = unique_name("newsp")
        new.create_view(v, replace=True)
        spine_cols = ", ".join(_q(smap[p]) for p in out_pk)
        out_cols = ", ".join(_q(p) for p in out_pk)
        return con.sql(
            f'SELECT * FROM {_q(v)} WHERE ({spine_cols}) NOT IN '
            f'(SELECT {out_cols} FROM {_q(name)} WHERE {_q(F_COL)} < {_ts(self.ctx.f)})'
        )

    # ─── offering a comprehensive recompute to the host (optional host capability) ─────

    def _offer(self, job: "ComprehensiveJob"):
        """Offer ``job`` to the host, which may run it elsewhere (Duckstring's Flock sends an over-envelope
        recompute to another engine). An optional host capability, read like ``record_lineage_write``: a host
        without ``dispatch_comprehensive`` computes locally. Returns the job's result relation, or ``None``
        for the local path. Only a recompute that happens anyway is offered, never an incremental run."""
        hook = getattr(self.ctx, "dispatch_comprehensive", None)
        return None if hook is None else hook(job)

    def _sql_result(self, out_pk):
        """A :meth:`sql` builder's result: offered to the host first (it is recomputed in full every run),
        else run locally."""
        if self._materialised is None:
            remote = self._offer(ComprehensiveJob(self, out_pk, "rows"))
            if remote is not None:
                self._materialised = remote
        return self._materialise()

    # ─── compute (the shared ΔO step behind .merge() and .append()) ───────────────

    def _compute(self, out_pk, name: str, *, ivm: bool = True, key_filter: bool = True, offer: str | None = None,
                 consolidate: bool = True):
        """Compose this DAG's change once. Returns ``(kind, rel)``: ``("comprehensive", o_prime)`` (whole
        output recomputed, clean rows — the caller diffs it against the stored main), ``("incremental",
        delta)`` (the Z-set ΔO, user cols + ``_duckstring_d``), ``("empty", None)`` (nothing changed), or
        ``("state", state)`` when an ``offer="aggregate"`` recompute was run by the host and returned the
        aggregate's accumulator state. Validates ``.select`` is present for a joined DAG and that it includes
        the PK. ``offer`` (``"rows"`` or ``"aggregate"``) is what a wholesale recompute is offered to the host
        as (see :meth:`_offer`); ``None`` never offers. ``consolidate=False`` leaves a join's ΔO unconsolidated,
        for a writer that consolidates what it writes anyway (``apply_zset``, ``append_zset``); otherwise it is
        consolidated once, after the output pipeline (which is row-local, so it commutes), over the output
        columns rather than every leaf's, unless it is insert-only (nothing can cancel, and a weight-aware
        writer or aggregate reads repeated rows as their sum); ``"always"`` consolidates that too, for the scans,
        which read rows rather than weights.

        Two orthogonal strategy flags, both default ``True`` (the normal path), exposed on the terminals as
        manual escapes — reach for them only when you've measured the default hurts a specific build:

        - **``ivm``** — *reuse the incremental machinery*. ``True`` composes the Z-set delta through the
          operator DAG (skips recomputing unchanged subtrees). ``False`` ignores deltas entirely and
          recomputes the whole output with plain full-table joins, diffed against the stored main (the
          comprehensive path) — the escape for when the delta logic is counterproductive, short of dropping
          to raw ``.sql()``.
        - **``key_filter``** — *push changed keys into composed operands*. A delta term joins a small side (a
          delta or a flip set) to a current state; when that state is itself a join, ``True`` filters the leaf
          owning the join key to the small side's keys first, tightening outer joins on the way (DuckDB can't
          move a join below an outer join itself). ``False`` leaves the subtree whole; the join
          still restricts it, just later. (No effect when ``ivm=False``.)"""
        # Absent output (dropped by a delete, or a genuine bootstrap) ⇒ recompute whole: the incremental
        # path only *appends* ΔO to the changelog and never reads the sink, so composing a partial delta
        # onto a nonexistent log would persist just that delta as a false bootstrap. `name not in read_meta`
        # (not raw table-existence — a young merge main has only a changelog pre-checkpoint) is the signal.
        # See plans/deletes.md. `ivm=False` is the manual sibling escape.
        self._insert_only = False
        if not ivm or name not in read_meta(self.ctx.con):
            return self._comprehensive(out_pk, offer)
        self._key_filter = key_filter
        self._prepare_leaves()
        state = self._compile(self._root)
        if state.is_full:  # a coverage miss, a changed overwrite source, or a source over its p threshold
            return self._comprehensive(out_pk, offer, state)
        if not state.changed:
            return "empty", None
        delta_rel = self._project_delta(state)
        cols = [c for c in delta_rel.columns if c != D_COL]
        self._require_pk(out_pk, cols)
        self._insert_only = state.insert_only  # the row pipeline keeps it: it only drops or rewrites rows
        if consolidate and isinstance(self._root, _Join) and not (state.insert_only and consolidate != "always"):
            projected = unique_name("proj")  # a source's own delta is consolidated already
            delta_rel.create_view(projected, replace=True)
            delta_rel = self.ctx.con.sql(f"SELECT * FROM {_q(self._consolidate(projected, cols))}")
        return "incremental", delta_rel

    def _comprehensive(self, out_pk, offer: str | None, state: _NodeState | None = None):
        """The wholesale recompute: offered to the host first, else built locally (from ``state`` when the
        compile already produced it)."""
        if offer is not None:
            remote = self._offer(ComprehensiveJob(self, out_pk, offer))
            if remote is not None:
                if offer == "aggregate":
                    return "state", remote
                self._require_pk(out_pk, remote.columns)
                return "comprehensive", remote
        o_prime = self._project_current(state) if state is not None else self._full_join()
        self._require_pk(out_pk, o_prime.columns)
        return "comprehensive", o_prime

    def _full_join(self, spine_rel=None):
        """The clean current output (filter + projection applied), recomputed from the current source
        states — the comprehensive recompute, also reused by ``.sql``/``.schema``/aggregate-rescan. With a
        ``spine_rel`` the leftmost leaf is backed by that relation (the append spine-PK fast path)."""
        return self.ctx.con.sql(self._full_join_sql(spine_rel))

    def _full_join_sql(self, spine_rel=None) -> str:
        self._prepare_leaves()
        cols, current = self._compile_current(self._root, spine_rel)
        return self._pipeline_sql(current, cols, is_delta=False)

    def _query_sql(self, leaf_sql) -> str:
        """This builder's full result as **one self-contained DuckDB query**: the comprehensive recompute's
        views become ``NOT MATERIALIZED`` CTEs (so the planner inlines them, as it does the views), and each
        source's base read becomes ``leaf_sql(leaf)``, a query over that source's current state with the same
        columns as ``ctx.read_table(leaf.ref)``. A :meth:`sql` builder wraps its query over the parent's."""
        if self._sql_of is not None:
            parent, query, alias = self._sql_of
            return (f"WITH {_q(alias)} AS NOT MATERIALIZED ({parent._query_sql(leaf_sql)}) "
                    f"SELECT * FROM ({query}) AS {_q('_duckstring_sql')}")
        self._view_log = log = []
        try:
            final = self._full_join_sql()
        finally:
            self._view_log = None
        ctes = ", ".join(f"{_q(name)} AS NOT MATERIALIZED ({leaf_sql(leaf) if leaf is not None else sql})"
                         for name, sql, leaf in log)
        return f"WITH {ctes} {final}"

    def _key_source(self, name: str):
        """The leaf and column an output column ``name`` passes through from **verbatim**, or ``None`` when it
        can't be traced exactly (computed, renamed through several selects, or after ``.sql()``). Restricting
        that leaf to some values of the column restricts the output to exactly the rows with those values,
        because joins, filters and computed columns all work row by row on the leaf's rows."""
        if self._is_sql:
            return None
        self._prepare_leaves()
        selects = [i for i, (kind, _) in enumerate(self._ops) if kind == "select"]
        if len(selects) > 1:
            return None
        start = selects[0] if selects else -1
        if any(kind == "mutate" and name in payload for kind, payload in self._ops[start + 1:]):
            return None  # replaced or added after the projection
        output = [leaf for leaf in self._output_leaves()]
        if selects:
            for item in _select_items(self._ops[start][1]):
                for leaf in output:
                    m = _passthrough_re(self._alias_for(leaf)).match(item.strip())
                    if m and (m.group(4) or m.group(2)) == name and m.group(2) in self._bare_cols(leaf):
                        return leaf, m.group(2)
            return None
        if any(kind == "mutate" and name in payload for kind, payload in self._ops):
            return None
        for leaf in output:  # the bare * output takes the leftmost copy of a name
            if name in self._bare_cols(leaf):
                return leaf, name
        return None

    def _output_leaves(self) -> list[_Source]:
        """The leaves whose columns reach the output (not the right side of a semi or anti join)."""
        def walk(node):
            if isinstance(node, _Source):
                return [node]
            return walk(node.left) + ([] if node.how in _LEFT_ONLY else walk(node.right))

        return walk(self._root)

    def _query_leaves(self) -> list[_Source]:
        """The sources :meth:`_query_sql` reads (through a :meth:`sql` chain to the composed DAG)."""
        return self._sql_of[0]._query_leaves() if self._sql_of is not None else self._leaves()

    # ─── leaf bookkeeping ────────────────────────────────────────────────────────

    def _leaves(self) -> list[_Source]:
        return self._root.leaves()

    def _prepare_leaves(self) -> None:
        """Assign each leaf its effective alias (explicit, else positional ``s{i}`` by left-to-right order)
        and validate uniqueness. Called at the start of every compile."""
        leaves = self._leaves()
        self._alias_of = {}
        self._states = {}
        seen = set()
        for i, leaf in enumerate(leaves):
            a = leaf.alias or f"s{i}"
            if a in seen:
                raise BuildError(f"duplicate source alias '{a}' — give each .trickle(...) a distinct .alias()")
            seen.add(a)
            self._alias_of[id(leaf)] = a

    def _alias_for(self, leaf: _Source) -> str:
        return self._alias_of[id(leaf)]

    def _bare_cols(self, leaf: _Source) -> list[str]:
        """A source's columns, from the host's ``source_columns`` hook where it has one (a binding, without
        the read :meth:`read_table` does), else from the read."""
        if id(leaf) not in self._cols_cache:
            hook = getattr(self.ctx, "source_columns", None)
            cols = hook(leaf.ref) if hook is not None else self.ctx.read_table(leaf.ref).columns
            self._cols_cache[id(leaf)] = list(cols)
        return self._cols_cache[id(leaf)]

    @property
    def _spine(self) -> _Source:
        leaves = self._leaves()
        return leaves[0]

    @property
    def _spine_delta(self):
        return self._spine.threaded_delta

    def _spine_delta_value(self):
        s = self._spine
        return s.threaded_delta if s.threaded_delta is not None else self.ctx.read_delta(s.ref)

    def _aliases_set(self) -> set[str]:
        return set(self._alias_of.values())

    # ─── compile: current-only (no deltas) ───────────────────────────────────────

    def _compile_current(self, node, spine_override=None):
        """Build the view(s) for ``node``'s full current state and return ``(cols, view)``. ``spine_override``
        (a relation) backs the leftmost leaf instead of its source read (the fast path)."""
        if isinstance(node, _Source):
            a = self._alias_for(node)
            if spine_override is not None:
                cols = [f"{a}.{c}" for c in spine_override.columns]
                base = unique_name("ovr")
                spine_override.create_view(base, replace=True)
                bare = spine_override.columns
            else:
                rel = self.ctx.read_table(node.ref)
                bare = list(rel.columns)
                cols = [f"{a}.{c}" for c in bare]
                base = unique_name("src")
                rel.create_view(base, replace=True)
                if self._view_log is not None:
                    self._view_log.append((base, None, node))
            sel = ", ".join(f'{_q(c)} AS {_q(f"{a}.{c}")}' for c in bare)
            return cols, self._view(f"SELECT {sel} FROM {_q(base)}")
        # _Join — the leftmost leaf lives in the left subtree, so the override goes left.
        lcols, lcur = self._compile_current(node.left, spine_override)
        rcols, rcur = self._compile_current(node.right, None)
        cols, cur = self._join_view(node, lcols, lcur, rcols, rcur)
        return cols, cur

    def _join_view(self, node, lcols, lcur, rcols, rcur, *, weight=None, how=None):
        """Emit one join view over the two child views. ``weight`` (``+1``/``−1``) appends a ``_duckstring_d``
        column (for delta terms); ``None`` is a plain state join. ``how`` overrides the node's join type (a
        tightened outer join, :meth:`_pushed`). Returns ``(out_cols, view_name)``."""
        out_cols = lcols if node.how in _LEFT_ONLY else lcols + rcols
        pairs = self._resolve_pairs(node)
        cond = " AND ".join(f'L.{_q(lq)} = R.{_q(rq)}' for lq, rq in pairs)
        sel = ", ".join(f'L.{_q(c)} AS {_q(c)}' for c in lcols)
        if node.how not in _LEFT_ONLY:
            sel += ", " + ", ".join(f'R.{_q(c)} AS {_q(c)}' for c in rcols)
        if weight is not None:
            sel += f", {int(weight)} AS {_q(D_COL)}"
        sql = f"SELECT {sel} FROM {_q(lcur)} L {_JOIN_SQL[how or node.how]} {_q(rcur)} R ON {cond}"
        return out_cols, self._view(sql)

    # ─── compile: full (current + delta) ─────────────────────────────────────────

    def _compile(self, node) -> _NodeState:
        state = self._compile_source(node) if isinstance(node, _Source) else self._compile_join(node)
        self._states[id(node)] = state  # the delta terms read their operands' subtrees back from here
        return state

    def _compile_source(self, node: _Source) -> _NodeState:
        a = self._alias_for(node)
        bare = self._bare_cols(node)
        cols = [f"{a}.{c}" for c in bare]
        sel = ", ".join(f'{_q(c)} AS {_q(f"{a}.{c}")}' for c in bare)

        def current() -> str:
            base = unique_name("src")
            self.ctx.read_table(node.ref).create_view(base, replace=True)
            return self._view(f"SELECT {sel} FROM {_q(base)}")

        delta = node.threaded_delta if node.threaded_delta is not None else self.ctx.read_delta(node.ref)
        if delta.is_full:
            return _NodeState(cols, current, None, True, True)
        if delta.is_empty():
            return _NodeState(cols, current, None, False, False)
        if self._over_threshold(node.ref, delta, node.p):
            return _NodeState(cols, current, None, True, True)
        dbase = unique_name("dsrc")
        delta.zset.create_view(dbase, replace=True)
        dsel = ", ".join(f'{_q(c)} AS {_q(f"{a}.{c}")}' for c in bare) + f", {_q(D_COL)} AS {_q(D_COL)}"
        return _NodeState(cols, current, self._view(f"SELECT {dsel} FROM {_q(dbase)}"), True, False,
                          insert_only=getattr(delta, "insert_only", False))

    def _compile_join(self, node: _Join) -> _NodeState:
        ls = self._compile(node.left)
        rs = self._compile(node.right)
        out_cols = ls.cols if node.how in _LEFT_ONLY else ls.cols + rs.cols
        is_full = ls.is_full or rs.is_full
        changed = ls.changed or rs.changed

        def current() -> str:
            return self._join_view(node, ls.cols, ls.current, rs.cols, rs.current)[1]

        if is_full or not changed:
            return _NodeState(out_cols, current, None, changed, is_full)
        # A child join's delta is an inline view, re-evaluated at every reference. When this join reads it more
        # than once, down a chain the bottom delta would run 2^depth times; a delta is small, so compute it once.
        for left, child, own, sibling, other in ((True, node.left, ls, node.right, rs),
                                                 (False, node.right, rs, node.left, ls)):
            if isinstance(child, _Join) and own.delta is not None and self._reads_twice(node, left, sibling, other):
                own.delta = self._scratch_table(own.delta)
        # Insert-only when one side changed, by inserts only, and the join is one pass from it: its one term
        # multiplies +1 rows by the other side's current rows. (Two changed sides, or a change on the side an
        # outer, semi or anti join pads or tests, can retract.)
        insert_only = ((ls.insert_only and rs.delta is None and node.how in _ONE_PASS_LEFT)
                       or (rs.insert_only and ls.delta is None and node.how in _ONE_PASS_RIGHT))
        return _NodeState(out_cols, current, self._join_delta(node, ls, rs), True, False, insert_only=insert_only)

    def _reads_twice(self, node: _Join, left: bool, sibling, other: _NodeState) -> bool:
        """Whether ``node``'s delta terms read one side's delta (the ``left`` one, or the right) more than once.
        Once when the other side (``sibling``, compiled to ``other``) is unchanged and the join is one pass from
        this side (:meth:`_one_pass`), unless the delta's keys are also pushed into the other side's subtree.
        Otherwise more than once: ``δL ⋈ δR``, and the existence terms of the semi, anti and outer joins."""
        if other.delta is not None:
            return True
        if node.how not in (_ONE_PASS_LEFT if left else _ONE_PASS_RIGHT):
            return True
        return self._key_filter and isinstance(sibling, _Join)

    # ─── the join delta (plans/trickle-join-delta.md) ────────────────────────────

    def _join_delta(self, node: _Join, ls: _NodeState, rs: _NodeState) -> str:
        """δ(L ⋈ R) as a sum of terms over the current states and the deltas, unconsolidated (the root
        consolidates once). Inner: ``δL ⋈ R' + L' ⋈ δR − δL ⋈ δR``. The existence parts (``semi``/``anti``,
        and the NULL-padded rows of the outer joins) are :meth:`_exists_delta`. An unchanged side has no delta,
        so its terms aren't generated."""
        pairs = self._resolve_pairs(node)
        lk, rk = [lq for lq, _ in pairs], [rq for _, rq in pairs]
        one = self._one_pass(node, ls, rs, lk, rk)
        if one is not None:
            return one
        terms = []
        if node.how in _MATCHED:
            terms += self._matched_terms(node, ls, rs, lk, rk)
        if node.how in ("left", "full"):
            unmatched = self._exists_delta(node.left, ls, lk, node.right, rs, rk, semi=False)
            terms.append(self._pad(unmatched, ls.cols, rs, right=True))
        if node.how in ("right", "full"):
            unmatched = self._exists_delta(node.right, rs, rk, node.left, ls, lk, semi=False)
            terms.append(self._pad(unmatched, rs.cols, ls, right=False))
        if node.how in _LEFT_ONLY:
            terms.append(self._exists_delta(node.left, ls, lk, node.right, rs, rk, semi=node.how == "semi"))
        return self._view(" UNION ALL BY NAME ".join(f"SELECT * FROM {_q(t)}" for t in terms))

    def _one_pass(self, node: _Join, ls: _NodeState, rs: _NodeState, lk, rk) -> str | None:
        """An outer join whose other side didn't change, in one pass. A left join is linear in its left side for
        a fixed right side, so with ``δR`` empty, ``δ(L ⟕ R) = δL ⟕ R'`` for any ``δL``, retractions included
        (and a right join likewise). The general terms would read ``δL`` twice, matched and padded. A full join
        isn't linear in either side (a new left row can take a right row's padding away), so it has none."""
        if node.how == "left" and ls.delta is not None and rs.delta is None:
            delta, dside, other, how = ls.delta, "L", self._pushed(node.right, rs, rk, ls.delta, lk), "LEFT JOIN"
            lview, rview = delta, other
        elif node.how == "right" and rs.delta is not None and ls.delta is None:
            delta, dside, other, how = rs.delta, "R", self._pushed(node.left, ls, lk, rs.delta, rk), "RIGHT JOIN"
            lview, rview = other, delta
        else:
            return None
        cond = " AND ".join(f"L.{_q(a)} = R.{_q(b)}" for a, b in zip(lk, rk, strict=True))
        sel = ", ".join(f"L.{_q(c)} AS {_q(c)}" for c in ls.cols) + ", "
        sel += ", ".join(f"R.{_q(c)} AS {_q(c)}" for c in rs.cols)
        return self._view(f"SELECT {sel}, {dside}.{_q(D_COL)} AS {_q(D_COL)} "
                          f"FROM {_q(lview)} L {how} {_q(rview)} R ON {cond}")

    def _matched_terms(self, node: _Join, ls: _NodeState, rs: _NodeState, lk, rk) -> list[str]:
        """The inner join's delta terms. A row's weight is the product of its operands' weights (a current
        state row weighs 1), so ``δL ⋈ δR`` enters negated: it is counted in both of the other terms."""
        d = _q(D_COL)
        cond = " AND ".join(f"L.{_q(a)} = R.{_q(b)}" for a, b in zip(lk, rk, strict=True))
        sel = ", ".join(f"L.{_q(c)} AS {_q(c)}" for c in ls.cols) + ", "
        sel += ", ".join(f"R.{_q(c)} AS {_q(c)}" for c in rs.cols)

        def term(lview, rview, weight):
            return self._view(f"SELECT {sel}, {weight} AS {d} FROM {_q(lview)} L JOIN {_q(rview)} R ON {cond}")

        out = []
        if ls.delta is not None:
            out.append(term(ls.delta, self._pushed(node.right, rs, rk, ls.delta, lk), f"L.{d}"))
        if rs.delta is not None:
            out.append(term(self._pushed(node.left, ls, lk, rs.delta, rk), rs.delta, f"R.{d}"))
        if ls.delta is not None and rs.delta is not None:
            out.append(term(ls.delta, rs.delta, f"-(L.{d} * R.{d})"))
        return out

    def _exists_delta(self, pnode, ps: _NodeState, pk, qnode, qs: _NodeState, qk, *, semi: bool) -> str:
        """The delta of ``P ⋉ Q`` (``semi``) or ``P ▷ Q`` (otherwise) over P's columns. With ``s = E' − E`` on
        the flip set ``F`` (the keys whose existence in Q changed):
        ``δ(P ⋉ Q) = δP ⋉ Q' + s·P'|F − s·δP|F`` and ``δ(P ▷ Q) = δP ▷ Q' − s·P'|F + s·δP|F``."""
        d = _q(D_COL)
        sel = ", ".join(f"P.{_q(c)} AS {_q(c)}" for c in ps.cols)
        parts = []
        if ps.delta is not None:
            cond = " AND ".join(f"P.{_q(a)} = Q.{_q(b)}" for a, b in zip(pk, qk, strict=True))
            qcur = self._pushed(qnode, qs, qk, ps.delta, pk)
            how = "SEMI JOIN" if semi else "ANTI JOIN"
            parts.append(f"SELECT {sel}, P.{d} AS {d} FROM {_q(ps.delta)} P {how} {_q(qcur)} Q ON {cond}")
        if qs.delta is not None:
            flips = self._flips(qnode, qs, qk)
            knames = [f"k{i}" for i in range(len(pk))]
            cond = " AND ".join(f"P.{_q(a)} = F.{k}" for a, k in zip(pk, knames, strict=True))
            # Restricted to δQ's keys rather than the flip set's (a superset, and the join with F narrows it), so
            # an outer join's matched term, which pushes the same keys into P, reads the same view (_pushed).
            pcur = self._pushed(pnode, ps, pk, qs.delta, qk)
            sign = "" if semi else "-"
            parts.append(f"SELECT {sel}, {sign}F.s AS {d} FROM {_q(pcur)} P JOIN {_q(flips)} F ON {cond}")
            if ps.delta is not None:
                sign = "-" if semi else ""
                parts.append(f"SELECT {sel}, {sign}F.s * P.{d} AS {d} FROM {_q(ps.delta)} P JOIN {_q(flips)} F ON {cond}")
        return self._view(" UNION ALL ".join(parts))

    def _flips(self, qnode, qs: _NodeState, qk) -> str:
        """The keys of ``δQ`` whose existence in Q changed, as ``k0…, s`` with ``s = +1`` for a key that gained
        its first match and ``−1`` for one that lost its last. From counts, not a prior state: ``n'`` counts Q'
        at the key and the prior count is ``n' − Σ δQ``. A key with a NULL part never matches, so it's left out."""
        d = _q(D_COL)
        knames = [f"k{i}" for i in range(len(qk))]
        ksel = ", ".join(f"{_q(c)} AS {k}" for c, k in zip(qk, knames, strict=True))
        notnull = " AND ".join(f"{_q(c)} IS NOT NULL" for c in qk)
        dsum = self._view(f"SELECT {ksel}, SUM({d}) AS ds FROM {_q(qs.delta)} WHERE {notnull} GROUP BY ALL")
        qcur = self._pushed(qnode, qs, qk, dsum, knames)
        on_q = " AND ".join(f"Q.{_q(c)} = D.{k}" for c, k in zip(qk, knames, strict=True))
        dk = ", ".join(f"D.{k}" for k in knames)
        count = f"SELECT {dk}, count(*) AS n FROM {_q(qcur)} Q JOIN {_q(dsum)} D ON {on_q} GROUP BY ALL"
        on_c = " AND ".join(f"D.{k} = C.{k}" for k in knames)
        n = "coalesce(C.n, 0)"
        return self._view(
            f"SELECT {dk}, CASE WHEN {n} > 0 THEN 1 ELSE -1 END AS s FROM {_q(dsum)} D LEFT JOIN ({count}) C "
            f"ON {on_c} WHERE ({n} > 0) <> ({n} - D.ds > 0)"
        )

    def _pad(self, view: str, cols, other: _NodeState, *, right: bool) -> str:
        """``view``'s rows (``cols`` + weight) with the other side's columns as typed NULLs, in output order
        (left columns first)."""
        rel = self.ctx.con.sql(f"SELECT * FROM {_q(other.current)}")
        types = dict(zip(rel.columns, (str(t) for t in rel.types), strict=True))
        nulls = ", ".join(f"CAST(NULL AS {types[c]}) AS {_q(c)}" for c in other.cols)
        have = ", ".join(_q(c) for c in cols)
        return self._view(f"SELECT {have + ', ' + nulls if right else nulls + ', ' + have}, {_q(D_COL)} FROM {_q(view)}")

    def _pushed(self, node, state: _NodeState, cols, src: str, src_cols) -> str:
        """``state.current`` for a term that joins it on ``cols`` to the small view ``src`` (keys in
        ``src_cols``), restricted to the rows that join can use. A leaf is returned as is: the join probes it
        once anyway. A subtree gets each key column's leaf filtered to ``src``'s keys first (DuckDB reorders inner
        joins itself but can't move one below an outer join). Every caller joins the result on ``cols`` by
        equality, so a row with a NULL-padded key column never contributes, and the outer joins on the way down
        are tightened to drop those rows (``_TIGHTEN``). Off with ``key_filter=False``."""
        if not self._key_filter or isinstance(node, _Source):
            return state.current
        filters: dict[int, list[tuple[str, int]]] = {}
        present: dict[int, set[str]] = {}  # join node id → the sides that must be present (not NULL-padded)
        for i, col in enumerate(cols):
            path = self._push_path(node, col)
            if path is None:
                continue
            leaf, sides = path
            filters.setdefault(id(leaf), []).append((col, i))
            for jid, side in sides:
                present.setdefault(jid, set()).add(side)
        if not filters:
            return state.current
        # Two terms can push the same keys into the same subtree (an outer join's matched rows and its padded
        # rows both read L' at δR's keys): each would re-read the subtree, for a large fact a full pass, so the
        # view is computed once when it's asked for a second time. Not while the views are being logged for a
        # comprehensive job, which wants them as SQL.
        memo = (id(node), tuple(cols), src, tuple(src_cols))
        if memo in self._pushed_memo:
            view, reused = self._pushed_memo[memo]
            if not reused and self._view_log is None:
                name = self._scratch_table(view)
                self.ctx.con.execute(f"CREATE OR REPLACE TEMP VIEW {_q(view)} AS SELECT * FROM {_q(name)}")
            self._pushed_memo[memo] = (view, True)
            return view
        ksel = ", ".join(f"{_q(c)} AS k{i}" for i, c in enumerate(src_cols))
        keys = self._view(f"SELECT DISTINCT {ksel} FROM {_q(src)}")
        view = self._filtered_current(node, filters, keys, present)
        self._pushed_memo[memo] = (view, False)
        return view

    def _push_path(self, node, col: str):
        """``(leaf, [(join id, side)])`` for the qualified column ``col``: the leaf that owns it and, for each
        join on the way down, which side holds it. ``None`` when it sits on the right of a semi or anti join."""
        alias = col.split(".", 1)[0]
        sides = []
        while isinstance(node, _Join):
            side = "left" if any(self._alias_for(leaf) == alias for leaf in node.left.leaves()) else "right"
            if (node.how, side) not in _TIGHTEN:
                return None
            sides.append((id(node), side))
            node = node.left if side == "left" else node.right
        return (node, sides) if self._alias_for(node) == alias else None

    def _filtered_current(self, node, filters, keys: str, present) -> str:
        """``node``'s current state rebuilt with ``filters`` (leaf id → ``[(col, i)]``: keep rows whose ``col``
        is among ``keys``' ``k{i}``) applied at the leaves, each join on the way tightened for the sides that
        must be present (``present``); an unfiltered subtree reuses its compiled view."""
        state = self._states[id(node)]
        if not any(id(leaf) in filters for leaf in node.leaves()):
            return state.current
        if isinstance(node, _Source):
            where = " AND ".join(f"{_q(c)} IN (SELECT k{i} FROM {_q(keys)})" for c, i in filters[id(node)])
            return self._view(f"SELECT * FROM {_q(state.current)} WHERE {where}")
        how = node.how
        for side in sorted(present.get(id(node), ())):
            how = _TIGHTEN[(how, side)]
        ls, rs = self._states[id(node.left)], self._states[id(node.right)]
        _, view = self._join_view(node, ls.cols, self._filtered_current(node.left, filters, keys, present),
                                  rs.cols, self._filtered_current(node.right, filters, keys, present), how=how)
        return view

    def _scratch_table(self, view: str) -> str:
        """``view`` computed once into a temp table, dropped after the terminal's write (:meth:`_chain`)."""
        name = unique_name("dagd")
        self.ctx.con.execute(f"CREATE TEMP TABLE {_q(name)} AS SELECT * FROM {_q(view)}")
        self._scratch.append(name)
        return name

    def _consolidate(self, view: str, cols) -> str:
        """``view`` consolidated by full row: one row per distinct image with its net weight, zeros dropped."""
        sel = ", ".join(_q(c) for c in cols)
        return self._view(
            f"SELECT {sel}, CAST(SUM({_q(D_COL)}) AS BIGINT) AS {_q(D_COL)} FROM {_q(view)} "
            f"GROUP BY {sel} HAVING SUM({_q(D_COL)}) <> 0"
        )

    # ─── join-key resolution ──────────────────────────────────────────────────────

    def _resolve_pairs(self, node: _Join):
        """Resolve each raw ``(left_name, right_name)`` of ``node`` to a ``(left_qualified, right_qualified)``
        column pair, by searching the left/right subtrees' leaf columns."""
        out = []
        for lname, rname in node.on_pairs:
            lq = self._resolve_col(node.left, lname, prefer_leftmost=True)
            rq = self._resolve_col(node.right, rname, prefer_leftmost=False)
            out.append((lq, rq))
        return out

    def _resolve_col(self, subtree, name: str, *, prefer_leftmost: bool) -> str:
        """Find the qualified column for ``name`` within ``subtree``. ``name`` may be ``alias.col`` (exact)
        or a bare column (unique across the subtree's leaves; ties broken by the leftmost leaf only when
        ``prefer_leftmost`` — else ambiguity raises)."""
        if "." in name:
            alias, _, col = name.partition(".")
            for leaf in subtree.leaves():
                if self._alias_for(leaf) == alias and col in self._bare_cols(leaf):
                    return f"{alias}.{col}"
            raise BuildError(f"join key '{name}' not found among the operand's sources")
        hits = [leaf for leaf in subtree.leaves() if name in self._bare_cols(leaf)]
        if not hits:
            raise BuildError(f"join key '{name}' not found among the operand's sources")
        if len(hits) > 1 and not prefer_leftmost:
            aliases = [self._alias_for(leaf) for leaf in hits]
            raise BuildError(
                f"join key '{name}' is ambiguous across {aliases} — qualify it as 'alias.{name}' or rename"
            )
        return f"{self._alias_for(hits[0])}.{name}"

    # ─── the output pipeline: filter / mutate / select ────────────────────────────
    #
    # The DAG (``self._root``) is compiled to a relation whose columns are leaf-qualified ``"alias.col"``; the
    # pipeline is then layered on top as nested subqueries (one per stage that a later stage references; it
    # collapses to a flat SELECT otherwise). Source columns stay ``alias.col``-qualified through the pipeline;
    # a mutated column is a bare name — the two namespaces never collide (``_qualify`` rewrites ``alias.col``
    # refs, leaves bare names alone). Every stage is a row-local deterministic map, so applying the pipeline
    # to the Z-set delta is identical to applying it to the full output and re-diffing — incrementally free.

    _STAR_RE = re.compile(r"^(\w+)\.\*$")
    _BARE_RE = re.compile(r'^(\w+)\.("?)(\w+)\2$')
    _AS_RE = re.compile(r'\bAS\s+("?)([A-Za-z_]\w*)\1\s*$', re.IGNORECASE)

    @staticmethod
    def _bare_of(col: str) -> str:
        """The output name a frame column maps to: the part after the dot for a qualified ``alias.col``, the
        name itself for a bare (mutated) column."""
        return col.split(".", 1)[1] if "." in col else col

    def _pipeline_sql(self, source_view: str, state_cols: list[str], *, is_delta: bool) -> str:
        """Apply the filter/mutate/select pipeline over ``source_view`` (columns = the qualified
        ``state_cols`` plus ``_duckstring_d`` when ``is_delta``) and return SQL yielding **bare-named** output
        columns (and ``_duckstring_d`` for a delta)."""
        aliases = self._aliases_set()
        frame = _q(source_view)
        cols = list(state_cols)  # frame columns: qualified "alias.col" plus bare mutated names
        dd = f", {_q(D_COL)}" if is_delta else ""
        terminal_select = False
        for kind, payload in self._ops:
            if kind == "filter":
                frame = f"(SELECT * FROM {frame} WHERE {_qualify(payload, aliases)})"
                terminal_select = False
            elif kind == "mutate":
                replaced = [c for c in cols if self._bare_of(c) in payload]
                excl = f" EXCLUDE ({', '.join(_q(c) for c in replaced)})" if replaced else ""
                adds = ", ".join(f"{_qualify(e, aliases)} AS {_q(n)}" for n, e in payload.items())
                frame = f"(SELECT *{excl}, {adds} FROM {frame})"
                cols = [c for c in cols if c not in replaced] + list(payload)
                terminal_select = False
            else:  # select — replaces the column set with the chosen (bare-named) list
                proj, cols = self._select_stage(payload, aliases)
                frame = f"(SELECT {proj}{dd} FROM {frame})"
                terminal_select = True
        if terminal_select:
            return f"SELECT * FROM {frame}"  # the select stage already bare-named the columns
        return f"SELECT {self._star_output(cols)}{dd} FROM {frame}"

    def _select_stage(self, projection: str, aliases: set[str]):
        """Compile one ``.select`` projection to ``(select_list_sql, output_bare_names)``: ``alias.col`` →
        bare ``col``, ``alias.*`` expands the leaf's columns, a computed item passes through (its name taken
        from a trailing ``AS``)."""
        out, names = [], []
        for item in _select_items(projection):
            s = item.strip()
            sm = self._STAR_RE.match(s)
            if sm and sm.group(1) in aliases:
                a = sm.group(1)
                leaf = next(leaf for leaf in self._leaves() if self._alias_for(leaf) == a)
                for c in self._bare_cols(leaf):
                    out.append(f'{_q(f"{a}.{c}")} AS {_q(c)}')
                    names.append(c)
                continue
            q = _qualify(item, aliases)
            bm = self._BARE_RE.match(s)
            if bm and bm.group(1) in aliases:
                out.append(f"{q} AS {_q(bm.group(3))}")
                names.append(bm.group(3))
            else:
                out.append(q)
                am = self._AS_RE.search(s)
                names.append(am.group(2) if am else s)  # best-effort name (used only if a stage follows)
        return ", ".join(out), names

    def _join_key_finder(self):
        """Union-find over the qualified columns equated by every ``on=`` in the DAG, so the ``*`` output can
        deduplicate equi-join keys (which carry the same value on both sides) while still rejecting any other
        name collision."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def walk(node) -> None:
            if isinstance(node, _Join):
                for lq, rq in self._resolve_pairs(node):
                    parent[find(lq)] = find(rq)
                walk(node.left)
                walk(node.right)

        walk(self._root)
        return find

    def _star_output(self, cols: list[str]) -> str:
        """Bare-name the surviving frame columns for the implicit ``*``: a qualified ``alias.col`` → bare
        ``col``, a mutated column passes through. Equi-join keys colliding on the bare name fold to the
        leftmost copy; any other bare-name collision raises (the dedup-keys rule)."""
        find = self._join_key_finder()
        by_bare: dict[str, list[str]] = {}
        for c in cols:
            by_bare.setdefault(self._bare_of(c), []).append(c)
        out = []
        for bare, members in by_bare.items():
            if len(members) > 1 and len({find(m) for m in members}) != 1:
                where = [m.split(".", 1)[0] for m in members if "." in m] or members
                raise BuildError(
                    f"column '{bare}' is ambiguous across {where} — name the survivors with .select(...) or "
                    f"rename via .mutate(); only equi-join keys are auto-deduplicated"
                )
            c = members[0]  # cols is left-to-right (leaf order), so this is the leftmost copy
            out.append(_q(c) if "." not in c else f"{_q(c)} AS {_q(bare)}")
        return ", ".join(out)

    def _project_current(self, state: _NodeState):
        return self.ctx.con.sql(self._pipeline_sql(state.current, state.cols, is_delta=False))

    def _project_delta(self, state: _NodeState):
        return self.ctx.con.sql(self._pipeline_sql(state.delta, state.cols, is_delta=True))

    # ─── misc ─────────────────────────────────────────────────────────────────────

    def _require_pk(self, out_pk, cols) -> None:
        missing = [c for c in out_pk if c not in cols]
        if missing:
            raise BuildError(
                f"the output is missing the PK column(s) {missing} — add them via .select(...), .mutate(...), "
                f"or leave the column in the bare * output"
            )

    def _chain(self, name: str, out_pk, changed: bool = True) -> "TrickleBuilder":
        """Thread the just-materialised output forward as a chainable in-run operand — its delta read back
        from the registry (same coverage rule as the published read_delta). ``changed`` records whether the
        write actually changed the output, surfaced on the returned handle via :meth:`was_changed`."""
        for t in self._scratch:  # the write is done, so nothing reads the materialised deltas any more
            self.ctx.con.execute(f"DROP TABLE IF EXISTS {_q(t)}")
        self._scratch = []
        self._pushed_memo = {}
        # Observed-lineage hook (optional host capability, like ``count_table``): a terminal just wrote
        # ``name`` straight to the registry, so tell the host — trickle/ stays dependency-free.
        hook = getattr(self.ctx, "record_lineage_write", None)
        if hook is not None:
            hook(name)
        hook = getattr(self.ctx, "record_change", None)  # likewise for the host's change tracking
        if hook is not None:
            hook(name, changed)
        con, previous_f, f = self.ctx.con, self.ctx.previous_f, self.ctx.f
        nxt = TrickleBuilder(self.ctx, name, _spine_delta=lambda: read_registry_delta(con, name, previous_f, f, out_pk))
        nxt._was_changed = changed
        return nxt

    def was_changed(self) -> bool:
        """On a builder returned by a terminal, whether that write changed the output table. Always ``True``
        after :meth:`aggregate` or :meth:`accumulate`. Use it to call ``pond.skip()``::

            out = pond.trickle("orders.order_line").join(...).merge("priced_line", pk="order_id")
            if not out.was_changed():
                pond.skip()
        """
        return self._was_changed

    def _source_rows(self, ref: str) -> int:
        """A source's current row count, from metadata where possible: a Trickle in this registry counts
        itself, and an external source uses the host's ``count_table`` if it has one. Only otherwise is the
        source read and counted, which for a large merge Trickle reconstructs its whole state."""
        from . import io as trickle

        con = self.ctx.con
        if (trickle.read_meta(con).get(ref) or {}).get("mode") in ("merge", "append"):
            return trickle.count_current(con, ref)
        counter = getattr(self.ctx, "count_table", None)
        if counter is not None:
            return int(counter(ref))
        return int(first_row(self.ctx.read_table(ref).aggregate("count(*)"))[0])

    def _over_threshold(self, ref: str, delta, p: float) -> bool:
        """Whether ``delta`` touches more than fraction ``p`` of ``ref``'s current rows (``p >= 1`` never
        trips and skips the count; ``p <= 0`` trips on any change)."""
        if p >= 1.0 or delta.is_full:
            return False
        total = self._source_rows(ref) or 0
        if total == 0:
            return False
        return delta.keys_count() > p * total

    def _view(self, sql: str) -> str:
        v = unique_name("dag")
        self.ctx.con.execute(f"CREATE OR REPLACE TEMP VIEW {_q(v)} AS {sql}")
        if self._view_log is not None:
            self._view_log.append((v, sql, None))
        return v


class ComprehensiveJob:
    """A terminal's comprehensive recompute, offered to the host's ``dispatch_comprehensive`` hook so it can
    run elsewhere. The host returns a relation with exactly :meth:`schema`'s columns, or ``None`` to compute
    locally.

    - ``kind`` is ``"rows"`` (the terminal's full output) or ``"aggregate"`` (the accumulator state of an
      ``.aggregate()``, one row per group, which the terminal installs and derives its columns from, so the
      large joined input never has to travel back).
    - :meth:`sql` is the whole job as one self-contained DuckDB query, given each source's read; a host
      running DuckDB elsewhere can run it unchanged.
    - :meth:`relation` is the same job over the local connection, not yet run (the local recompute).
    """

    def __init__(self, builder: TrickleBuilder, out_pk, kind: str = "rows") -> None:
        self.builder = builder
        self.ctx = builder.ctx
        self.out_pk = tuple(out_pk or ())
        self.kind = kind

    def leaves(self) -> list[_Source]:
        """The sources the job reads; each has ``.ref`` (what ``ctx.read_table`` takes) and an alias."""
        return self.builder._query_leaves()

    def columns(self, leaf: _Source) -> list[str]:
        """A leaf's columns, as ``ctx.read_table(leaf.ref)`` gives them."""
        b = self.builder
        while b._sql_of is not None:
            b = b._sql_of[0]
        return list(b._bare_cols(leaf))

    def queries(self) -> list[str]:
        """The user SQL the job runs: each :meth:`TrickleBuilder.sql` query in the chain, innermost first."""
        out, b = [], self.builder
        while b._sql_of is not None:
            out.append(b._sql_of[1])
            b = b._sql_of[0]
        return out[::-1]

    def sql(self, leaf_sql) -> str:
        """The job as one self-contained DuckDB query. ``leaf_sql(leaf)`` gives a query over a leaf's current
        state, with the same columns as ``ctx.read_table(leaf.ref)``."""
        body = self.builder._query_sql(leaf_sql)
        if self.kind == "aggregate":
            from .io import agg_rebuild_select

            src = "_duckstring_agg_input"
            agg = self.builder._agg
            return (f"WITH {_q(src)} AS NOT MATERIALIZED ({body}) "
                    f"{agg_rebuild_select(src, agg['by'], agg['metrics'], self.ctx.f)}")
        return body

    def relation(self):
        """The job over the local connection, bound but not run."""
        rel = self.builder._bound()
        if self.kind != "aggregate":
            return rel
        from .io import agg_rebuild_select

        src = unique_name("agginput")
        rel.create_view(src, replace=True)
        agg = self.builder._agg
        return self.ctx.con.sql(agg_rebuild_select(src, agg["by"], agg["metrics"], self.ctx.f))

    def schema(self) -> dict[str, str]:
        """The job's columns and DuckDB types, from binding it locally: what any engine's result must match."""
        rel = self.relation()
        return {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}
