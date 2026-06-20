"""The ``pond.trickle(...)`` builder — a DBSP-style **DAG of binary incremental joins** over Z-set sources.

A fluent builder that records an **operator DAG** (sources + binary equi-joins of any ``how``, plus a final
filter and projection) and maintains its output incrementally. Each join node is maintained by the single
**affected-key recompute** rule (``plans/trickle-dag.md``): for ``O = A ⋈ₖ B`` with deltas ``δA``, ``δB``,

    K = πₖ(δA) ∪ πₖ(δB)                 -- the join-key values that changed on either side
    δO = (O_new restricted to K)(+1) ⊎ (O_old restricted to K)(−1), consolidated

Restricting **both** inputs to ``key ∈ K`` before the join is sound for every join type (it is the semijoin
the join already performs) and is the key pre-filter — a small change never drives a full scan of the other
side. Re-evaluating each affected key's full output old-vs-new *is* the match-count logic for the outer
incomparables (a NULL-padded preserved row), so ``left``/``right``/``full``/``semi``/``anti`` are maintained
the same way as ``inner`` — no privileged spine, so a bushy ``(A⋈B)⋈(C⋈D)`` and outer joins compose freely.

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

from .io import D_COL, _q, _table_exists, normalize_pk, read_registry_delta, unique_name

_W = "_duckstring_w"  # scratch weight column for prior-state reconstruction (distinct from the Z-set D_COL)


class BuildError(ValueError):
    """The builder was misconfigured (a missing merge key / ``.select()``, an ambiguous join key, a
    malformed join)."""


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
    not-yet-published data plane)."""

    def __init__(self, ref: str, p: float, *, alias: str | None = None, threaded_delta=None) -> None:
        self.ref = ref
        self.p = p
        self.alias = alias
        self.threaded_delta = threaded_delta

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
    ``current``/``old`` (view names for the new and prior full states), ``delta`` (view name for the
    consolidated Z-set ΔO, or ``None``), ``changed`` and ``is_full``."""

    __slots__ = ("cols", "current", "old", "delta", "changed", "is_full")

    def __init__(self, cols, current, old, delta, changed, is_full) -> None:
        self.cols = cols
        self.current = current
        self.old = old
        self.delta = delta
        self.changed = changed
        self.is_full = is_full


class TrickleBuilder:
    """One handle into the build DAG. ``pond.trickle(ref)`` starts a DAG rooted at a source; :meth:`join`
    composes another (possibly itself composed) ``pond.trickle(...)`` operand as a binary join. ``.filter``/
    ``.select`` attach to the composed result; the terminals are :meth:`merge` / :meth:`append`."""

    def __init__(self, ctx, spine_ref: str, *, p: float = 0.3, _spine_delta=None) -> None:
        self.ctx = ctx
        self.spine_ref = spine_ref
        self.p = p
        self._root = _Source(spine_ref, p, threaded_delta=_spine_delta)
        self._filters: list[str] = []
        self._projection: str | None = None
        self._alias: str | None = None  # this node's alias (the spine's, for .select refs / the .sql table)
        self._materialised = None  # a full relation after .sql() → comprehensive mode (no incremental compute)
        self._agg = None  # {"by": (...), "metrics": {out: (kind, col, how)}} after .aggregate()
        self._agg_by: tuple[str, ...] | None = None
        self._key_filter = True  # set per-terminal from the .merge()/.append() key_filter flag
        # compile-scoped caches (rebuilt per terminal): leaf → alias, leaf → bare cols
        self._alias_of: dict[int, str] = {}
        self._cols_cache: dict[int, list[str]] = {}

    # ─── fluent surface ─────────────────────────────────────────────────────────

    def alias(self, name: str) -> "TrickleBuilder":
        """Name this node. On a **source** the parent's ``.select``/``.filter`` reference it by name instead
        of ``s0``/``s1``; on a builder you ``.sql()`` over, it's the name the query uses."""
        self._alias = name
        if isinstance(self._root, _Source):
            self._root.alias = name
        return self

    def join(self, dimension: "TrickleBuilder", *, on, how: str = "inner") -> "TrickleBuilder":
        """Equi-join another ``pond.trickle(...)`` operand on ``on`` (any column(s); a shared name, a list,
        or a ``{left: right}`` dict; a name may be ``alias.col`` to disambiguate). ``how`` ∈ ``inner``
        (default) / ``left`` / ``right`` / ``full`` / ``semi`` / ``anti`` — all maintained incrementally.

        The operand may itself be a join DAG (``(a⋈b)`` composed), so bushy and snowflake shapes are
        expressible. It must not already carry a ``.filter()``/``.select()``/``.aggregate()``/``.sql()`` —
        attach those to the composed result, or split via a downstream Trickle."""
        self._ensure_incremental("join")
        how = how.lower()
        if how not in _JOIN_SQL:
            raise BuildError(f"join(how={how!r}): one of {sorted(_JOIN_SQL)}")
        if not isinstance(dimension, TrickleBuilder):
            raise BuildError("join() takes another pond.trickle(...) operand")
        if dimension._materialised is not None or dimension._agg is not None:
            raise BuildError("join(): a .sql()/.aggregate() result can't be a join operand — do it downstream")
        if dimension._filters or dimension._projection is not None:
            raise BuildError(
                f"join('{dimension.spine_ref}'): a join operand can't carry its own .filter()/.select() — "
                f"attach those to the composed result, or materialise it with a downstream .merge()"
            )
        self._root = _Join(self._root, dimension._root, _join_pairs(on), how)
        return self

    def filter(self, predicate: str) -> "TrickleBuilder":
        """Restrict the output with a SQL boolean ``predicate`` over the composed sources (by ``s0``/``s1``/…
        or their ``.alias()`` names)."""
        self._ensure_incremental("filter")
        self._filters.append(predicate)
        return self

    def select(self, projection: str) -> "TrickleBuilder":
        """The output column list (a SQL select list). Required when the DAG has joins; it must include the
        output PK. Reference sources by ``s{i}`` (left-to-right leaf order) or their ``.alias()`` names."""
        self._ensure_incremental("select")
        self._projection = projection
        return self

    def _ensure_incremental(self, op: str) -> None:
        if self._materialised is not None:
            raise BuildError(
                f".{op}() isn't available after .sql() (the result is materialised, no longer a Z-set) — "
                f"compose joins/filters/projection before .sql(), or chain another .sql()"
            )
        if self._agg is not None:
            raise BuildError(
                f".{op}() can't follow .aggregate() — aggregate is terminal-bound to .merge(); do further "
                f"work in a downstream Trickle"
            )

    def group_by(self, by) -> "TrickleBuilder":
        """Ibis-shaped alias: ``.group_by(by).aggregate(**metrics)`` ≡ ``.aggregate(by=by, **metrics)``."""
        self._ensure_incremental("group_by")
        self._agg_by = normalize_pk(by)
        return self

    def aggregate(self, by=None, **metrics) -> "TrickleBuilder":
        """Group the composed output by ``by`` and maintain the ``metrics`` incrementally — a grouped merge
        Trickle keyed by ``by`` (the output ``pk`` defaults to it). Metrics are :mod:`duckstring.agg` specs
        (count / sum / mean / min / max / var / stddev). Terminal-bound to :meth:`merge`."""
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
                raise BuildError(f"aggregate metric '{out}' must be an agg.* spec (agg.count/sum/mean/min/max/var/stddev)")
            spec[out] = (m.kind, m.col, m.how)
        self._agg = {"by": by, "metrics": spec}
        return self

    def sql(self, query) -> "TrickleBuilder":
        """**The comprehensive escape hatch.** Collapse everything composed so far into one relation, expose
        it under this node's :meth:`alias` (or a generated name), run ``query`` over it, and return a builder
        in *comprehensive mode* — the home for anything outside the incremental op set (aggregation, window
        functions, ``DISTINCT``, set ops, …).

        It **breaks incremental compute** but **keeps incremental output**: the terminal :meth:`merge` still
        diffs the result against the prior main, so only changed rows reach the changelog. ``query`` is a SQL
        string, or — with Ibis installed — an Ibis expression compiled lazily via ``ibis.to_sql``."""
        ctx = self.ctx
        if self._agg is not None:
            raise BuildError(".sql() can't follow .aggregate() — aggregate is terminal-bound to .merge()")
        if self._alias is None:
            raise BuildError(".sql() needs a table name to reference — call .alias('t') first, then '… FROM t'")
        if self._materialised is None and isinstance(self._root, _Join) and self._projection is None:
            raise BuildError(
                "pond.trickle(...).join(...).sql(...): add .select(...) before .sql() so the joined columns "
                "are named for the query"
            )
        base = self._materialised if self._materialised is not None else self._full_join()
        table = self._alias
        base.create_view(table, replace=True)
        if not isinstance(query, str):  # an Ibis expression → compile to DuckDB SQL (ibis only imported here)
            import ibis

            query = str(ibis.to_sql(query, dialect="duckdb"))
        out_table = unique_name("sqlout")
        ctx.con.execute(f'CREATE OR REPLACE TEMP TABLE {_q(out_table)} AS {query}')
        out = TrickleBuilder(ctx, out_table)
        out._materialised = ctx.con.sql(f'SELECT * FROM {_q(out_table)}')
        return out

    def schema(self) -> dict[str, str]:
        """``{column: DuckDB type}`` for this node's current output — introspection, no execution."""
        rel = self._materialised if self._materialised is not None else self._full_join()
        return {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}

    def to_ibis_schema(self) -> dict[str, str]:
        """:meth:`schema` mapped to Ibis type-strings."""
        return {c: _duckdb_to_ibis(t) for c, t in self.schema().items()}

    # ─── terminals ──────────────────────────────────────────────────────────────

    def merge(self, name: str, *, pk=None, ivm: bool = True, key_filter: bool = True,
              retain_t=None, retain_n=None) -> "TrickleBuilder":
        """Compose ΔO from the changed sources (or recompute comprehensively) and apply it to the output
        **merge** Trickle ``name`` (clean main + Z-set changelog). ``pk`` (**required**, except after
        :meth:`aggregate` where it defaults to the group key) is the output identity / merge key.

        ``ivm`` / ``key_filter`` are the two strategy escapes (both default ``True`` — the normal path); see
        :meth:`_compute` for what they do. Use them only when you've measured that the default hurts a
        specific build.

        Returns a chainable :class:`TrickleBuilder` rooted at ``name`` so joins can be chained through
        intermediate materialisations **in one Ripple** — each ``.merge()`` persists the intermediate as a
        cross-run trace (the explicit short-circuit to per-run recomputation)."""
        ctx = self.ctx
        if self._agg is not None:
            from . import io as trickle

            by, metrics = self._agg["by"], self._agg["metrics"]
            out_pk = normalize_pk(pk) if pk is not None else by
            required = tuple(dict.fromkeys(by + tuple(c for _k, c, _h in metrics.values() if c is not None)))
            kind, rel = self._compute(required, name, ivm=ivm, key_filter=key_filter)
            needs_current = kind == "incremental" and any(k in ("min", "max") for k, _c, _h in metrics.values())
            current = self._full_join() if needs_current else None
            trickle.apply_aggregate(ctx.con, name, by, metrics, kind, rel, current, ctx.f,
                                    retain_t=retain_t, retain_n=retain_n)
            return self._chain(name, out_pk)
        out_pk = normalize_pk(pk)
        if not out_pk:
            raise BuildError(f"pond.trickle('{self.spine_ref}')...merge('{name}'): pass the output key, merge(pk=...)")
        if self._materialised is not None:  # comprehensive mode (post-.sql) → diff the relation vs prior main
            ctx.merge_table(name, self._materialised, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
            return self._chain(name, out_pk)
        kind, rel = self._compute(out_pk, name, ivm=ivm, key_filter=key_filter)
        if kind == "comprehensive":
            ctx.merge_table(name, rel, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
        elif kind == "incremental":
            ctx.apply_zset(name, rel, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
        # "empty": nothing changed (and no full read) → output unchanged, no write.
        return self._chain(name, out_pk)

    def append(
        self, name: str, *, pk=None, fail_on_conflict=True, log_drops=True, ivm: bool = True,
        key_filter: bool = True, retain_t=None, retain_n=None
    ) -> "TrickleBuilder":
        """Execute, writing the result to an **append** (insert-only history) Trickle ``name`` — for a
        *monotonic* transform (output rows only added, never updated/retracted). See the module docs and
        :func:`duckstring.trickle_io.append_zset` for the conflict semantics. ``ivm`` / ``key_filter`` are
        the strategy escapes (see :meth:`_compute`); ``ivm=False`` also disables the spine-PK fast path.

        **Spine-PK fast path** — when the output is keyed by the spine's own PK (a verbatim ``s0.<pk>``
        projection) *and* conflicts are both waived (``fail_on_conflict=False``) and unlogged
        (``log_drops=False``), a dimension delta cannot affect the result, so the builder enriches only the
        **new spine rows** with the **current** dimension states (an O(spine delta) lookup)."""
        ctx = self.ctx
        if self._agg is not None:
            raise BuildError(".append() can't follow .aggregate() — an aggregate updates groups; use .merge()")
        out_pk = normalize_pk(pk)
        from . import io as trickle

        if self._materialised is not None:  # comprehensive mode (post-.sql) → +1 the relation, append-filter
            trickle.append_zset(
                ctx.con, name, trickle._as_zset(self._materialised, 1), ctx.f, out_pk,
                fail_on_conflict=fail_on_conflict, log_drops=log_drops, retain_t=retain_t, retain_n=retain_n,
            )
            return self._chain(name, out_pk)

        spine_delta = self._spine_delta_value()
        if (ivm and isinstance(self._root, _Join) and not fail_on_conflict and not log_drops
                and self._spine_pk_passthrough(out_pk, spine_delta.pk)):
            candidate = self._full_join(spine_rel=self._new_spine_rows(spine_delta, name, out_pk))
            trickle.append_zset(
                ctx.con, name, trickle._as_zset(candidate, 1), ctx.f, out_pk,
                fail_on_conflict=False, log_drops=False, retain_t=retain_t, retain_n=retain_n,
            )
            return self._chain(name, out_pk)

        kind, rel = self._compute(out_pk, name, ivm=ivm, key_filter=key_filter)
        if kind != "empty":
            zset = trickle._as_zset(rel, 1) if kind == "comprehensive" else rel
            trickle.append_zset(
                ctx.con, name, zset, ctx.f, out_pk,
                fail_on_conflict=fail_on_conflict, log_drops=log_drops, retain_t=retain_t, retain_n=retain_n,
            )
        return self._chain(name, out_pk)

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
        history — the only rows that can produce a new append row when the output is spine-PK keyed."""
        con = self.ctx.con
        new = spine_delta.upserts
        smap = self._spine_pk_passthrough(out_pk, spine_delta.pk)
        if not _table_exists(con, name):
            return new
        v = unique_name("newsp")
        new.create_view(v, replace=True)
        spine_cols = ", ".join(_q(smap[p]) for p in out_pk)
        out_cols = ", ".join(_q(p) for p in out_pk)
        return con.sql(f'SELECT * FROM {_q(v)} WHERE ({spine_cols}) NOT IN (SELECT {out_cols} FROM {_q(name)})')

    # ─── compute (the shared ΔO step behind .merge() and .append()) ───────────────

    def _compute(self, out_pk, name: str, *, ivm: bool = True, key_filter: bool = True):
        """Compose this DAG's change once. Returns ``(kind, rel)``: ``("comprehensive", o_prime)`` (whole
        output recomputed, clean rows — the caller diffs it against the stored main), ``("incremental",
        delta)`` (the Z-set ΔO, user cols + ``_duckstring_d``), or ``("empty", None)`` (nothing changed).
        Validates ``.select`` is present for a joined DAG and that it includes the PK.

        Two orthogonal strategy flags, both default ``True`` (the normal path), exposed on the terminals as
        manual escapes — reach for them only when you've measured the default hurts a specific build:

        - **``ivm``** — *reuse the incremental machinery*. ``True`` composes the Z-set delta through the
          operator DAG (skips recomputing unchanged subtrees). ``False`` ignores deltas entirely and
          recomputes the whole output with plain full-table joins, diffed against the stored main (the
          comprehensive path) — the escape for when the delta logic is counterproductive, short of dropping
          to raw ``.sql()``.
        - **``key_filter``** — *bound the per-join recompute to the changed keys*. ``True`` pre-filters both
          join inputs to ``key ∈ K`` (the affected keys). ``False`` keeps the delta composition but skips the
          ``IN (…)`` restriction (joins the full new/old states and diffs) — useful when the change is large
          enough to trip ``p`` anyway, so the filter buys nothing. (No effect when ``ivm=False``.)"""
        if isinstance(self._root, _Join) and self._projection is None:
            raise BuildError(
                f"pond.trickle('{self.spine_ref}').join(...): a joined DAG needs .select(...) to name the "
                f"output columns (and include the PK)"
            )
        if not ivm:
            # ivm=False escape: ignore deltas, recompute the whole output and diff vs the stored main.
            o_prime = self._full_join()
            self._require_pk(out_pk, o_prime.columns)
            return "comprehensive", o_prime
        self._key_filter = key_filter
        self._prepare_leaves()
        state = self._compile(self._root)
        if state.is_full:
            o_prime = self._project_current(state)
            self._require_pk(out_pk, o_prime.columns)
            return "comprehensive", o_prime
        if not state.changed:
            return "empty", None
        delta_rel = self._project_delta(state)
        self._require_pk(out_pk, [c for c in delta_rel.columns if c != D_COL])
        return "incremental", delta_rel

    def _full_join(self, spine_rel=None):
        """The clean current output (filter + projection applied), recomputed from the current source
        states — the comprehensive recompute, also reused by ``.sql``/``.schema``/aggregate-rescan. With a
        ``spine_rel`` the leftmost leaf is backed by that relation (the append spine-PK fast path)."""
        self._prepare_leaves()
        cols, current = self._compile_current(self._root, spine_rel)
        state = _NodeState(cols, current, current, None, False, False)
        return self._project_current(state)

    # ─── leaf bookkeeping ────────────────────────────────────────────────────────

    def _leaves(self) -> list[_Source]:
        return self._root.leaves()

    def _prepare_leaves(self) -> None:
        """Assign each leaf its effective alias (explicit, else positional ``s{i}`` by left-to-right order)
        and validate uniqueness. Called at the start of every compile."""
        leaves = self._leaves()
        self._alias_of = {}
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
        if id(leaf) not in self._cols_cache:
            self._cols_cache[id(leaf)] = list(self.ctx.read_table(leaf.ref).columns)
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
            sel = ", ".join(f'{_q(c)} AS {_q(f"{a}.{c}")}' for c in bare)
            return cols, self._view(f"SELECT {sel} FROM {_q(base)}")
        # _Join — the leftmost leaf lives in the left subtree, so the override goes left.
        lcols, lcur = self._compile_current(node.left, spine_override)
        rcols, rcur = self._compile_current(node.right, None)
        cols, cur = self._join_view(node, lcols, lcur, rcols, rcur)
        return cols, cur

    def _join_view(self, node, lcols, lcur, rcols, rcur, *, weight=None):
        """Emit one join view over the two child views. ``weight`` (``+1``/``−1``) appends a ``_duckstring_d``
        column (for delta terms); ``None`` is a plain state join. Returns ``(out_cols, view_name)``."""
        out_cols = lcols if node.how in _LEFT_ONLY else lcols + rcols
        pairs = self._resolve_pairs(node)
        cond = " AND ".join(f'L.{_q(lq)} = R.{_q(rq)}' for lq, rq in pairs)
        sel = ", ".join(f'L.{_q(c)} AS {_q(c)}' for c in lcols)
        if node.how not in _LEFT_ONLY:
            sel += ", " + ", ".join(f'R.{_q(c)} AS {_q(c)}' for c in rcols)
        if weight is not None:
            sel += f", {int(weight)} AS {_q(D_COL)}"
        sql = f"SELECT {sel} FROM {_q(lcur)} L {_JOIN_SQL[node.how]} {_q(rcur)} R ON {cond}"
        return out_cols, self._view(sql)

    # ─── compile: full (current + old + delta) ───────────────────────────────────

    def _compile(self, node) -> _NodeState:
        if isinstance(node, _Source):
            return self._compile_source(node)
        return self._compile_join(node)

    def _compile_source(self, node: _Source) -> _NodeState:
        a = self._alias_for(node)
        rel = self.ctx.read_table(node.ref)
        bare = list(rel.columns)
        self._cols_cache[id(node)] = bare
        cols = [f"{a}.{c}" for c in bare]
        base = unique_name("src")
        rel.create_view(base, replace=True)
        sel = ", ".join(f'{_q(c)} AS {_q(f"{a}.{c}")}' for c in bare)
        current = self._view(f"SELECT {sel} FROM {_q(base)}")

        delta = node.threaded_delta if node.threaded_delta is not None else self.ctx.read_delta(node.ref)
        is_full = delta.is_full
        changed = is_full
        delta_view = None
        old = current
        if not is_full:
            if delta.is_empty():
                return _NodeState(cols, current, current, None, False, False)
            changed = True
            if self._over_threshold(node.ref, delta, node.p):
                return _NodeState(cols, current, current, None, True, True)
            dbase = unique_name("dsrc")
            delta.zset.create_view(dbase, replace=True)
            dsel = ", ".join(f'{_q(c)} AS {_q(f"{a}.{c}")}' for c in bare) + f", {_q(D_COL)} AS {_q(D_COL)}"
            delta_view = self._view(f"SELECT {dsel} FROM {_q(dbase)}")
            old = self._reconstruct_old(cols, current, delta_view)
        return _NodeState(cols, current, old, delta_view, changed, is_full)

    def _compile_join(self, node: _Join) -> _NodeState:
        ls = self._compile(node.left)
        rs = self._compile(node.right)
        out_cols = ls.cols if node.how in _LEFT_ONLY else ls.cols + rs.cols
        is_full = ls.is_full or rs.is_full
        changed = ls.changed or rs.changed
        _, current = self._join_view(node, ls.cols, ls.current, rs.cols, rs.current)
        if is_full or not changed:
            return _NodeState(out_cols, current, current, None, changed, is_full)
        _, old = self._join_view(node, ls.cols, ls.old, rs.cols, rs.old)
        delta = self._join_delta(node, ls, rs, out_cols)
        return _NodeState(out_cols, current, old, delta, True, False)

    def _join_delta(self, node: _Join, ls: _NodeState, rs: _NodeState, out_cols) -> str:
        """δ(L ⋈ R) by the affected-key recompute: K = the changed sides' join-key values; recompute the
        join restricted to ``key ∈ K`` over the new states (+1) and the old states (−1), and consolidate.
        With ``key_filter=False`` the ``K`` restriction is skipped — the same diff over the *full* new/old
        states (correct, just unpruned)."""
        pairs = self._resolve_pairs(node)
        k = self._affected_keys(ls, rs, pairs) if self._key_filter else None
        new = self._restricted_join(node, ls.cols, ls.current, rs.cols, rs.current, pairs, k, 1)
        old = self._restricted_join(node, ls.cols, ls.old, rs.cols, rs.old, pairs, k, -1)
        cols_sql = ", ".join(_q(c) for c in out_cols)
        return self._view(
            f"SELECT {cols_sql}, CAST(SUM({_q(D_COL)}) AS BIGINT) AS {_q(D_COL)} "
            f"FROM (SELECT * FROM {_q(new)} UNION ALL BY NAME SELECT * FROM {_q(old)}) "
            f"GROUP BY {cols_sql} HAVING SUM({_q(D_COL)}) <> 0"
        )

    def _affected_keys(self, ls: _NodeState, rs: _NodeState, pairs) -> str:
        """A view of the changed join-key values: the left-side key columns of ``δL`` ∪ the right-side key
        columns of ``δR`` (aliased to a common ``k0,k1,…``)."""
        knames = [f"k{i}" for i in range(len(pairs))]
        ksel = ", ".join(knames)
        parts = []
        if ls.changed and ls.delta is not None:
            sel = ", ".join(f'{_q(lq)} AS {kn}' for (lq, _rq), kn in zip(pairs, knames, strict=True))
            parts.append(f"SELECT {sel} FROM {_q(ls.delta)}")
        if rs.changed and rs.delta is not None:
            sel = ", ".join(f'{_q(rq)} AS {kn}' for (_lq, rq), kn in zip(pairs, knames, strict=True))
            parts.append(f"SELECT {sel} FROM {_q(rs.delta)}")
        return self._view(f"SELECT DISTINCT {ksel} FROM ({' UNION ALL '.join(parts)})")

    def _restricted_join(self, node, lcols, lview, rcols, rview, pairs, kview, weight) -> str:
        """A join view of two states weighted ``weight`` — the affected-key recompute term. ``kview`` (the
        affected keys) pre-filters **both** inputs to ``key ∈ K``; ``None`` (``key_filter=False``) joins the
        full states unrestricted."""
        if kview is not None:
            lkey = ", ".join(_q(lq) for lq, _rq in pairs)
            rkey = ", ".join(_q(rq) for _lq, rq in pairs)
            ksel = ", ".join(f"k{i}" for i in range(len(pairs)))
            lview = self._view(f"SELECT * FROM {_q(lview)} WHERE ({lkey}) IN (SELECT {ksel} FROM {_q(kview)})")
            rview = self._view(f"SELECT * FROM {_q(rview)} WHERE ({rkey}) IN (SELECT {ksel} FROM {_q(kview)})")
        _, view = self._join_view(node, lcols, lview, rcols, rview, weight=weight)
        return view

    def _reconstruct_old(self, cols, current, delta_view) -> str:
        """``prior = consolidate(current(+1) ⊎ −delta)`` — the source's state before this run's change."""
        sel = ", ".join(_q(c) for c in cols)
        return self._view(
            f"SELECT {sel} FROM ("
            f"SELECT {sel}, 1 AS {_q(_W)} FROM {_q(current)} "
            f"UNION ALL BY NAME SELECT {sel}, -{_q(D_COL)} AS {_q(_W)} FROM {_q(delta_view)}"
            f") GROUP BY {sel} HAVING SUM({_q(_W)}) > 0"
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

    # ─── projection / filter ──────────────────────────────────────────────────────

    def _filter_clause(self) -> str:
        if not self._filters:
            return ""
        aliases = self._aliases_set()
        return " WHERE " + " AND ".join(_qualify(p, aliases) for p in self._filters)

    def _projection_sql(self, state: _NodeState) -> str:
        """The output select list: qualified internal columns mapped to **bare** output names. With no
        ``.select`` (a single-source build) the leftmost leaf's columns pass through bare."""
        aliases = self._aliases_set()
        if self._projection is None:
            spine = self._spine
            a = self._alias_for(spine)
            return ", ".join(f'{_q(f"{a}.{c}")} AS {_q(c)}' for c in self._bare_cols(spine))
        out = []
        star = re.compile(r"^(\w+)\.\*$")
        bare = re.compile(r'^(\w+)\.("?)(\w+)\2$')
        for item in _select_items(self._projection):
            s = item.strip()
            sm = star.match(s)
            if sm and sm.group(1) in aliases:
                a = sm.group(1)
                leaf = next(leaf for leaf in self._leaves() if self._alias_for(leaf) == a)
                out += [f'{_q(f"{a}.{c}")} AS {_q(c)}' for c in self._bare_cols(leaf)]
                continue
            q = _qualify(item, aliases)
            bm = bare.match(s)
            if bm and bm.group(1) in aliases:
                out.append(f"{q} AS {_q(bm.group(3))}")
            else:
                out.append(q)
        return ", ".join(out)

    def _project_current(self, state: _NodeState):
        proj = self._projection_sql(state)
        return self.ctx.con.sql(f"SELECT {proj} FROM {_q(state.current)}{self._filter_clause()}")

    def _project_delta(self, state: _NodeState):
        proj = self._projection_sql(state)
        return self.ctx.con.sql(
            f"SELECT {proj}, {_q(D_COL)} FROM {_q(state.delta)}{self._filter_clause()}"
        )

    # ─── misc ─────────────────────────────────────────────────────────────────────

    def _require_pk(self, out_pk, cols) -> None:
        missing = [c for c in out_pk if c not in cols]
        if missing:
            raise BuildError(f".select(...) must include the PK column(s) {missing}")

    def _chain(self, name: str, out_pk) -> "TrickleBuilder":
        """Thread the just-materialised output forward as a chainable in-run operand — its delta read back
        from the registry (same coverage rule as the published read_delta)."""
        threaded = read_registry_delta(self.ctx.con, name, self.ctx.previous_f, self.ctx.f, out_pk)
        return TrickleBuilder(self.ctx, name, _spine_delta=threaded)

    def _over_threshold(self, ref: str, delta, p: float) -> bool:
        """Whether ``delta`` touches more than fraction ``p`` of ``ref``'s current rows (``p >= 1`` never
        trips and skips the count; ``p <= 0`` trips on any change)."""
        if p >= 1.0 or delta.is_full:
            return False
        total = self.ctx.read_table(ref).aggregate("count(*) AS n").fetchone()[0] or 0
        if total == 0:
            return False
        return delta.keys_count() > p * total

    def _view(self, sql: str) -> str:
        v = unique_name("dag")
        self.ctx.con.execute(f"CREATE OR REPLACE TEMP VIEW {_q(v)} AS {sql}")
        return v
