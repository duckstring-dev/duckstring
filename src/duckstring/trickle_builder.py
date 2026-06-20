"""The ``pond.trickle(...)`` builder — DBSP-style incremental joins over Z-set sources.

A fluent builder that records a **tiny op graph** (a spine source + direct equi-joins to dimension
sources, plus filters and a projection) and maintains its output incrementally by composing the **Z-set
delta** of each changed source through the join (see ``plans/trickle-dbsp.md``). Because deletions are
full-row ``-1`` tuples (not key tombstones), a join may be on **any** key — there is one ``.join()`` and
no FK=PK constraint.

The incremental answer for an n-way join is the standard IVM telescoping sum: for sources ``[s0, s1, …]``
in evaluation order, ``ΔO = Σᵢ (s0_new · … · s_{i-1}_new · δᵢ · s_{i+1}_old · … · sn_old)``. A term whose
source did not change (``δᵢ = ∅``) drops out, so an unchanged source costs nothing; ``_old`` reconstruction
(``current − delta``) is needed only for a *changed* source that sits after another changed source.

When any source can't supply a clean delta — a **bootstrap**, a **coverage-miss**, a **changed overwrite
Ripple**, or a delta over its change-fraction threshold ``p`` — the whole output is recomputed and diffed
against the **materialised prior output** (the last-written *main*, read not recomputed). That is always
correct because the main is the one prior state we always have. An *unchanged* overwrite Ripple is a free
stable history operand; only a *changed* one forces the comprehensive path.
"""

from __future__ import annotations

import re

from .trickle_io import D_COL, _q, _table_exists, normalize_pk, read_registry_delta, unique_name


class BuildError(ValueError):
    """The builder was misconfigured (a missing merge key / ``.select()``, a malformed join)."""


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


# Supported join types → their DuckDB keyword. **Spine-grained** ones (inner/left/semi/anti) keep the
# output one-contribution-per-spine-row, so they compose in a multi-way star and are maintained
# incrementally (telescoping for all-inner; affected-spine-row recompute when any non-inner is present).
# right/full preserve the *other* side (unmatched dim rows), so they must be the only join and recompute
# comprehensively (correct, not yet incrementalised).
_JOIN_SQL = {"inner": "JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL JOIN",
             "semi": "SEMI JOIN", "anti": "ANTI JOIN"}
_SPINE_GRAINED = {"inner", "left", "semi", "anti"}


def _cols(on) -> tuple[str, ...]:
    return (on,) if isinstance(on, str) else tuple(on)


def _join_pairs(on) -> list[tuple[str, str]]:
    """Normalise ``on`` to ``[(spine_col, dim_col), …]``. A str/list names columns shared by both sides;
    a dict maps spine columns to dim columns (when the names differ)."""
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


class TrickleBuilder:
    """One node of the build graph. ``pond.trickle(ref)`` starts a graph rooted at the **spine** source;
    :meth:`join` attaches a dimension directly to the spine.

    ``_spine_delta`` is set when this builder is the value returned by an upstream :meth:`merge` — the
    spine is then a just-materialised in-run Trickle whose change is already known, so it is read from
    that handle (the registry) instead of the not-yet-published data plane."""

    def __init__(self, pond, spine_ref: str, *, p: float = 0.3, _spine_delta=None) -> None:
        self.pond = pond
        self.spine_ref = spine_ref
        self.p = p  # this source's change-fraction threshold (see Pond.trickle)
        # (dim_ref, pairs, p, alias, how)
        self._joins: list[tuple[str, list[tuple[str, str]], float, str | None, str]] = []
        self._filters: list[str] = []
        self._projection: str | None = None
        self._alias: str | None = None  # this node's name in .select/.filter and as the .sql() table
        self._spine_delta = _spine_delta  # set by an upstream .merge() → chained in-run operand
        self._materialised = None  # a full relation after .sql() → comprehensive mode (no incremental compute)
        self._agg = None  # {"by": (...), "metrics": {out: (kind, col)}} after .aggregate() → grouped output
        self._agg_by: tuple[str, ...] | None = None  # set by .group_by(), consumed by a following .aggregate()

    def alias(self, name: str) -> "TrickleBuilder":
        """Name this node. On a **source** the parent's ``.select``/``.filter`` reference it by name instead
        of ``s0``/``s1``; on the builder you ``.sql()`` over, it's the name the query uses (the Ibis idiom).
        ``s0``/``s1`` stay the fallback for unaliased sources, so naming is opt-in and reordering ``.join()``
        calls no longer silently remaps positional column references."""
        self._alias = name
        return self

    def join(self, dimension: "TrickleBuilder", *, on, how: str = "inner") -> "TrickleBuilder":
        """Equi-join a **dimension** (another bare ``pond.trickle(...)``) directly to the spine on ``on``
        (any column(s); see :func:`_join_pairs`). ``how`` ∈ ``inner`` (default) / ``left`` / ``right`` /
        ``full`` / ``semi`` / ``anti``. The dimension must be a bare source — a snowflake/chain isn't in the
        op set (do the deeper hop in a downstream Trickle).

        ``inner``/``left``/``semi``/``anti`` are **spine-grained** (one contribution per spine row): they
        compose in a multi-way star and are maintained incrementally. ``right``/``full`` preserve unmatched
        dimension rows, so they must be the **only** join and recompute comprehensively (correct, not yet
        incrementalised)."""
        self._ensure_incremental("join")
        how = how.lower()
        if how not in _JOIN_SQL:
            raise BuildError(f"join(how={how!r}): one of {sorted(_JOIN_SQL)}")
        if not isinstance(dimension, TrickleBuilder):
            raise BuildError("join() takes another pond.trickle(...) source as the dimension")
        if dimension._joins or dimension._filters or dimension._projection is not None:
            raise BuildError(
                f"join('{dimension.spine_ref}'): a dimension must be a bare source — a snowflake/transitive "
                f"chain isn't in the builder's op set; do the deeper hop in a downstream Trickle"
            )
        self._joins.append((dimension.spine_ref, _join_pairs(on), dimension.p, dimension._alias, how))
        return self

    def filter(self, predicate: str) -> "TrickleBuilder":
        """Restrict the output with a SQL boolean ``predicate`` (over the joined sources, by ``s0``/``s1``/…
        or their ``.alias()`` names)."""
        self._ensure_incremental("filter")
        self._filters.append(predicate)
        return self

    def select(self, projection: str) -> "TrickleBuilder":
        """The output column list (a SQL select list). Required when the graph has joins; it must include
        the output PK. The spine is ``s0`` and the i-th joined dimension is ``s{i+1}`` — or use the
        ``.alias()`` names (``o.id``, ``p."col"``)."""
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
        (``agg.count()`` / ``agg.sum(col)`` / ``agg.mean(col)`` — the distributive/algebraic set, maintained
        from the delta alone). Terminal-bound to :meth:`merge`; ``.append`` and further joins/filters/selects
        after it are out of the op set (use a downstream Trickle)."""
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
                raise BuildError(f"aggregate metric '{out}' must be an agg.* spec (agg.count/agg.sum/agg.mean)")
            spec[out] = (m.kind, m.col)
        self._agg = {"by": by, "metrics": spec}
        return self

    def sql(self, query) -> "TrickleBuilder":
        """**The comprehensive escape hatch.** Collapse everything composed so far into one relation, expose
        it under this node's :meth:`alias` (or a generated name), run ``query`` over it, and return a builder
        in *comprehensive mode* — the home for anything outside the incremental op set (aggregation, window
        functions, ``DISTINCT``, set ops, …).

        It **breaks incremental compute** (after ``.sql()`` there are no joins/key-prefilter/fast-path
        shortcuts — the data is fully materialised) but **keeps incremental output**: the terminal
        :meth:`merge` still diffs the result against the prior main, so only changed rows reach the changelog.
        ``query`` is a SQL string, or — if you have Ibis installed — an Ibis expression, compiled lazily via
        ``ibis.to_sql(..., dialect="duckdb")`` (see :meth:`to_ibis_schema`)."""
        pond = self.pond
        if self._agg is not None:
            raise BuildError(".sql() can't follow .aggregate() — aggregate is terminal-bound to .merge()")
        if self._alias is None:
            raise BuildError(".sql() needs a table name to reference — call .alias('t') first, then '… FROM t'")
        if self._materialised is None and self._joins and self._projection is None:
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
        # Materialise the result into a stable temp table so it can't rebind if the alias is reused later.
        out_table = unique_name("sqlout")
        pond.con.execute(f'CREATE OR REPLACE TEMP TABLE {_q(out_table)} AS {query}')
        out = TrickleBuilder(pond, out_table)
        out._materialised = pond.con.sql(f'SELECT * FROM {_q(out_table)}')
        return out

    def schema(self) -> dict[str, str]:
        """``{column: DuckDB type}`` for this node's current output — introspection, no execution. Most
        useful on a projected or just-``.merge()``'d builder; pairs with :meth:`to_ibis_schema`."""
        rel = self._materialised if self._materialised is not None else self._full_join()
        return {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True)}

    def to_ibis_schema(self) -> dict[str, str]:
        """:meth:`schema` mapped to Ibis type-strings — feed it to ``ibis.table(builder.to_ibis_schema(),
        name="x")`` to build an Ibis expression, then hand that expression back to ``.sql()``. No Ibis
        dependency (returns a plain dict). Raises on a DuckDB type with no Ibis mapping."""
        return {c: _duckdb_to_ibis(t) for c, t in self.schema().items()}

    def merge(self, name: str, *, pk=None, retain_t=None, retain_n=None) -> "TrickleBuilder":
        """Execute: compose ΔO from the changed sources' Z-sets (or recompute comprehensively) and apply it
        to the output **merge** Trickle ``name`` (clean main + Z-set changelog). ``pk`` (**required**, except
        after :meth:`aggregate` where it defaults to the group key) is the output identity / merge key — it
        must be genuinely unique in the output (a many-to-many join that fans out past it corrupts the keyed
        main).

        Returns a :class:`TrickleBuilder` rooted at the just-materialised ``name``, so joins can be chained
        through intermediate materialisations **in one Ripple** —
        ``a.join(b).merge("ab", pk=…).join(c).merge("abc", pk=…)``. Each ``.merge()`` stores its output's
        trace (registry main + changelog), so a later run that changes only ``c`` reuses the stored ``ab``
        instead of recomputing ``a⋈b`` — the same win as splitting into a downstream Trickle, without the
        boilerplate (and without the parallelism a separate Ripple under a Wave would allow — a chain is
        explicitly sequential). The returned handle carries ``ab``'s in-run delta (read from the registry),
        so the downstream join composes without a round-trip through the not-yet-published data plane."""
        pond = self.pond
        if self._agg is not None:  # grouped aggregate output (keyed by the group columns)
            from . import trickle_io as trickle

            by, metrics = self._agg["by"], self._agg["metrics"]
            out_pk = normalize_pk(pk) if pk is not None else by
            required = tuple(dict.fromkeys(by + tuple(c for _k, c in metrics.values() if c is not None)))
            kind, rel = self._compute(required, name)
            trickle.apply_aggregate(pond.con, name, by, metrics, kind, rel, pond.f,
                                    retain_t=retain_t, retain_n=retain_n)
            return self._chain(name, out_pk)
        out_pk = normalize_pk(pk)
        if not out_pk:
            raise BuildError(f"pond.trickle('{self.spine_ref}')...merge('{name}'): pass the output key, merge(pk=...)")
        if self._materialised is not None:  # comprehensive mode (post-.sql) → diff the relation vs prior main
            pond.merge_table(name, self._materialised, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
            return self._chain(name, out_pk)
        kind, rel = self._compute(out_pk, name)
        if kind == "comprehensive":
            pond.merge_table(name, rel, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
        elif kind == "incremental":
            pond.apply_zset(name, rel, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
        # "empty": nothing changed (and no full read) → output unchanged, no write.
        return self._chain(name, out_pk)

    def append(
        self, name: str, *, pk=None, fail_on_conflict=True, log_drops=True, retain_t=None, retain_n=None
    ) -> "TrickleBuilder":
        """Execute, writing the result to an **append** (insert-only history) Trickle ``name`` instead of a
        merge main+changelog — the right terminal for a *monotonic* transform (output rows are only ever
        added, never updated or retracted), e.g. enriching an append-only fact stream with stable/SCD dims.

        An insert-only table can't reflect a change to the past, so a retraction in ΔO, or a ``+1`` row whose
        ``pk`` is already in history with a **different** image, is a conflict (an identical-image collision
        is a benign idempotent skip). ``fail_on_conflict=True`` (default — correctness first) raises; ``False``
        drops the offending rows (history wins) and, with ``log_drops``, records them in a ``{name}__droplog``
        companion (published alongside the table, like ``__changelog``). ``pk`` is optional but recommended:
        with it set the conflict checks engage;
        ``pk=None`` + ``fail_on_conflict=False`` skips them entirely (fast, sound only when duplicates and
        past-changes are impossible by construction). Returns a chainable handle like :meth:`merge`.

        **Spine-PK fast path** — when the output is keyed by the spine's own PK (passed through the projection
        by identity) *and* conflicts are both waived (``fail_on_conflict=False``) and unlogged
        (``log_drops=False``), a change to an existing output row is dropped-and-forgotten either way, so a
        dimension delta cannot affect the result. The builder skips the dimension deltas entirely and enriches
        only the **new spine rows** with the **current** dimension states — turning a small dimension churn
        that would otherwise drive a spine scan into an O(spine delta) lookup. Detection is conservative (only
        a verbatim ``s0.<pk>`` projection); anything else falls back to the general path, which is always
        correct."""
        pond = self.pond
        if self._agg is not None:
            raise BuildError(".append() can't follow .aggregate() — an aggregate updates groups; use .merge()")
        out_pk = normalize_pk(pk)
        from . import trickle_io as trickle

        if self._materialised is not None:  # comprehensive mode (post-.sql) → +1 the relation, append-filter
            trickle.append_zset(
                pond.con, name, trickle._as_zset(self._materialised, 1), pond.f, out_pk,
                fail_on_conflict=fail_on_conflict, log_drops=log_drops, retain_t=retain_t, retain_n=retain_n,
            )
            return self._chain(name, out_pk)

        spine_delta = self._spine_delta if self._spine_delta is not None else pond.read_delta(self.spine_ref)
        spine_grained = all(t[4] in _SPINE_GRAINED for t in self._joins)
        if (self._joins and spine_grained and not fail_on_conflict and not log_drops
                and self._spine_pk_passthrough(out_pk, spine_delta.pk)):
            candidate = self._full_join(spine_rel=self._new_spine_rows(spine_delta, name, out_pk))
            trickle.append_zset(
                pond.con, name, trickle._as_zset(candidate, 1), pond.f, out_pk,
                fail_on_conflict=False, log_drops=False, retain_t=retain_t, retain_n=retain_n,
            )
            return self._chain(name, out_pk)

        kind, rel = self._compute(out_pk, name, spine_delta=spine_delta)
        if kind != "empty":
            # A comprehensive recompute is the whole current output (clean rows) → tag it +1; an incremental
            # ΔO already carries its ±weights. append_zset filters retractions / pk conflicts either way.
            zset = trickle._as_zset(rel, 1) if kind == "comprehensive" else rel
            trickle.append_zset(
                pond.con, name, zset, pond.f, out_pk,
                fail_on_conflict=fail_on_conflict, log_drops=log_drops, retain_t=retain_t, retain_n=retain_n,
            )
        return self._chain(name, out_pk)

    def _spine_pk_passthrough(self, out_pk, spine_pk):
        """If every output-PK column is a verbatim ``s0.<col>`` pass-through of the **spine's** PK (so the
        output is keyed by the spine's identity), return ``{out_col: spine_col}``; else ``None``. Conservative
        — it bails on any computed / non-``s0`` / ambiguous PK column, so a false positive (which would wrongly
        drop rows) is impossible; a false negative just forgoes the optimization."""
        spine_pk = tuple(spine_pk or ())
        if not spine_pk or not out_pk or self._projection is None:
            return None
        pat = _passthrough_re(self._alias or "s0")  # match against the spine's effective alias
        proj = {}
        for item in _select_items(self._projection):
            m = pat.match(item)
            if m:
                proj[(m.group(4) or m.group(2))] = m.group(2)  # output name → spine column
        smap = {}
        for p in out_pk:
            if p not in proj:
                return None
            smap[p] = proj[p]
        return smap if set(smap.values()) == set(spine_pk) else None

    def _new_spine_rows(self, spine_delta, name: str, out_pk):
        """The spine rows that arrived/changed this run whose (mapped) PK is **not yet** in the output history
        — the only rows that can produce a new append row when the output is spine-PK keyed."""
        con = self.pond.con
        new = spine_delta.upserts  # +1 net spine rows (clean user columns)
        smap = self._spine_pk_passthrough(out_pk, spine_delta.pk)
        if not _table_exists(con, name):
            return new  # bootstrap → every row is new
        v = unique_name("newsp")
        new.create_view(v, replace=True)
        spine_cols = ", ".join(_q(smap[p]) for p in out_pk)
        out_cols = ", ".join(_q(p) for p in out_pk)
        return con.sql(f'SELECT * FROM {_q(v)} WHERE ({spine_cols}) NOT IN (SELECT {out_cols} FROM {_q(name)})')

    def _compute(self, out_pk, name: str, spine_delta=None):
        """Compose this build graph's change once. Returns ``(kind, rel)``:
        ``("comprehensive", o_prime)`` — the whole output recomputed (clean rows), when any source can't
        supply a clean delta or exceeds its threshold ``p``; ``("incremental", unioned)`` — the Z-set ΔO
        (user cols + ``_duckstring_d``); ``("empty", None)`` — nothing changed. Shared by :meth:`merge` and
        :meth:`append`. Validates ``.select`` is present for a joined graph and includes the PK."""
        pond = self.pond
        if self._joins and self._projection is None:
            raise BuildError(
                f"pond.trickle('{self.spine_ref}').join(...): a joined graph needs .select(...) to name the "
                f"output columns (and include the PK)"
            )
        hows = [t[4] for t in self._joins]
        outer = any(h in ("right", "full") for h in hows)
        if outer and len(self._joins) > 1:
            raise BuildError(
                "a right/full outer join must be the only join in a builder (it preserves unmatched rows, so "
                "it can't anchor a star) — do the multi-way part in a downstream Trickle"
            )
        refs = [self.spine_ref] + [t[0] for t in self._joins]
        ps = [self.p] + [t[2] for t in self._joins]
        # The spine of a chained builder is a just-materialised in-run Trickle: use its known delta, not the
        # (unpublished) data-plane read. Dimensions are always bare sources read the normal way.
        if spine_delta is None:
            spine_delta = self._spine_delta if self._spine_delta is not None else pond.read_delta(self.spine_ref)
        deltas = [spine_delta] + [pond.read_delta(r) for r in refs[1:]]

        over = any(self._over_threshold(r, d, p) for r, d, p in zip(refs, deltas, ps, strict=True))
        # Comprehensive: any source full-reads, a source is over its threshold, or a right/full outer join
        # (preserves unmatched rows → not incrementalised here). _full_join honours every join type.
        if outer or any(d.is_full for d in deltas) or over:
            o_prime = self._full_join()
            self._require_pk(out_pk, o_prime.columns)
            return "comprehensive", o_prime
        states = [self._state_views(r, d) for r, d in zip(refs, deltas, strict=True)]
        if not any(st["changed"] for st in states):
            return "empty", None
        if all(h == "inner" for h in hows):
            # All-inner star → the bilinear telescoping sum (one term per changed source).
            terms = [self._term(i, states) for i, st in enumerate(states) if st["changed"]]
            delta_rel = pond.con.sql(" UNION ALL BY NAME ".join(f"({t})" for t in terms))
        else:
            # A spine-grained non-inner star (left/semi/anti, possibly mixed with inner) → recompute the
            # affected spine rows over new (+1) and old (−1) states and diff.
            delta_rel = self._spine_recompute(states)
        self._require_pk(out_pk, [c for c in delta_rel.columns if c != D_COL])
        return "incremental", delta_rel

    def _spine_recompute(self, states):
        """ΔO for a spine-grained star with a non-inner join. Recompute the full star (honouring each join's
        ``how``) for the **affected spine rows** — those in the spine delta or matching a changed dimension's
        delta keys — over the new states (``+1``) and old states (``−1``), and diff. Over-inclusion is safe
        (an unchanged row's old and new outputs cancel); the filter just keeps the recompute bounded by the
        delta rather than the whole spine."""
        affected = self._affected_filter(states)
        aliases = self._aliases()
        new = self._recompute_select([st["current"] for st in states], aliases, 1, affected)
        old = self._recompute_select([st["prior"] for st in states], aliases, -1, affected)
        return self.pond.con.sql(f"({new}) UNION ALL BY NAME ({old})")

    def _recompute_select(self, backing, aliases, sign: int, spine_filter: str) -> str:
        projection = self._projection or f"{aliases[0]}.*"
        where = f" WHERE {' AND '.join(self._filters)}" if self._filters else ""
        return (
            f"SELECT {projection}, {sign} AS {_q(D_COL)} "
            f"FROM {self._from_clause(backing, aliases, spine_filter)}{where}"
        )

    def _affected_filter(self, states) -> str:
        """A SQL predicate over the spine's own columns selecting the rows whose output could have changed:
        the changed spine rows themselves, plus any spine row whose join key matches a changed dimension's
        delta. (Evaluated inside the spine subquery, so it names bare spine columns + the delta views.)"""
        clauses = []
        sp = states[0]
        if sp["changed"]:
            cols = ", ".join(_q(c) for c in sp["user"])
            clauses.append(f"({cols}) IN (SELECT {cols} FROM {_q(sp['delta'])})")
        for i, st in enumerate(states[1:], start=1):
            if st["changed"]:
                _dim_ref, pairs, _p, _alias, _how = self._joins[i - 1]
                sc = ", ".join(_q(s) for s, _d in pairs)
                dc = ", ".join(_q(d) for _s, d in pairs)
                clauses.append(f"({sc}) IN (SELECT {dc} FROM {_q(st['delta'])})")
        return " OR ".join(clauses) if clauses else "1=0"

    def _require_pk(self, out_pk, cols) -> None:
        missing = [c for c in out_pk if c not in cols]
        if missing:
            raise BuildError(f".select(...) must include the PK column(s) {missing}")

    def _chain(self, name: str, out_pk) -> "TrickleBuilder":
        """Thread the just-materialised output forward as a chainable in-run operand — its delta read back
        from the registry (same coverage rule as the published read_delta), so a downstream ``.join(...)``
        composes without a round-trip through the not-yet-published data plane."""
        threaded = read_registry_delta(self.pond.con, name, self.pond.previous_f, self.pond.f, out_pk)
        return TrickleBuilder(self.pond, name, _spine_delta=threaded)

    # ─── internals ────────────────────────────────────────────────────────────

    def _over_threshold(self, ref: str, delta, p: float) -> bool:
        """Whether ``delta`` touches more than fraction ``p`` of ``ref``'s current rows (``p >= 1`` never
        trips and skips the count; ``p <= 0`` trips on any change). A full read is handled separately."""
        if p >= 1.0 or delta.is_full:
            return False
        total = self.pond.read_table(ref).aggregate("count(*) AS n").fetchone()[0] or 0
        if total == 0:
            return False
        return delta.keys_count() > p * total

    def _state_views(self, ref: str, delta) -> dict:
        """Register the current state, and (if the source changed) the delta and reconstructed prior state,
        as uniquely-named views. ``prior = consolidate(current(+1) ⊎ −delta)`` — only built when changed
        (an unchanged source's prior is its current)."""
        con = self.pond.con
        current_rel = self.pond.read_table(ref)
        user = list(current_rel.columns)
        cur = unique_name("cur")
        current_rel.create_view(cur, replace=True)
        st = {"user": user, "current": cur, "changed": not delta.is_empty()}
        if not st["changed"]:
            st["prior"] = cur
            st["delta"] = None
            return st
        dview = unique_name("dlt")
        delta.zset.create_view(dview, replace=True)
        st["delta"] = dview
        sel = ", ".join(_q(c) for c in user)
        prior = unique_name("pri")
        con.execute(
            f'CREATE OR REPLACE TEMP VIEW {_q(prior)} AS '
            f'SELECT {sel} FROM ('
            f'  SELECT {sel}, 1 AS {_q(D_COL)} FROM {_q(cur)} '
            f'  UNION ALL BY NAME SELECT {sel}, -{_q(D_COL)} AS {_q(D_COL)} FROM {_q(dview)}'
            f') GROUP BY {sel} HAVING SUM({_q(D_COL)}) > 0'
        )
        st["prior"] = prior
        return st

    def _aliases(self) -> list[str]:
        """The SQL alias for each source — the spine then each dimension. A source's ``.alias()`` name if it
        set one, else the positional ``s{j}`` fallback (so unaliased graphs read exactly as before)."""
        out = [self._alias or "s0"]
        out += [alias or f"s{i + 1}" for i, (_ref, _pairs, _p, alias, _how) in enumerate(self._joins)]
        return out

    def _from_clause(self, backing: list[str], aliases: list[str], spine_filter: str | None = None) -> str:
        """The ``FROM spine JOIN dim …`` clause, source ``j`` SQL-aliased ``aliases[j]`` and backed by
        ``backing[j]`` (a view). ``spine_filter`` (a SQL predicate over the spine's own columns) restricts
        the spine *before* the join — the key pre-filter (see :meth:`_term`)."""
        s0a = aliases[0]
        s0 = (f'(SELECT * FROM {_q(backing[0])} WHERE {spine_filter})' if spine_filter else _q(backing[0]))
        parts = [f'{s0} {s0a}']
        for d, (_dim_ref, pairs, _p, _alias, how) in enumerate(self._joins):
            a = aliases[d + 1]
            cond = " AND ".join(f'{s0a}.{_q(sc)} = {a}.{_q(dc)}' for sc, dc in pairs)
            parts.append(f'{_JOIN_SQL[how]} {_q(backing[d + 1])} {a} ON {cond}')
        return " ".join(parts)

    def _term(self, i: int, states: list[dict]) -> str:
        """Telescoping term where source ``i`` contributes its delta: sources before ``i`` use their *new*
        (current) state, source ``i`` its delta, sources after ``i`` their *old* (prior) state. The term's
        weight is source ``i``'s ``_duckstring_d`` (every other operand is a clean +1 state).

        **Key pre-filter** — when a *dimension* (``i >= 1``) is the delta, the (large) spine is restricted
        to the rows whose join key matches that dimension's changed keys before the join, so a small
        dimension change doesn't drive a full spine scan. Sound for *any* equi-join (it is the semi-join
        the join already performs) and inclusive of deletes (a retraction carries the full image, so its
        join key is in the delta). This is the general-purpose performance lever; the join key need not be
        the dimension's PK."""
        aliases = self._aliases()
        backing = []
        for j, st in enumerate(states):
            if j < i:
                backing.append(st["current"])
            elif j == i:
                backing.append(st["delta"])
            else:
                backing.append(st["prior"])
        spine_filter = None
        if i >= 1:  # a dimension changed → pre-filter the spine to its affected keys
            _dim_ref, pairs, _p, _alias, _how = self._joins[i - 1]
            sc = ", ".join(_q(s) for s, _d in pairs)
            dc = ", ".join(_q(d) for _s, d in pairs)
            spine_filter = f"({sc}) IN (SELECT {dc} FROM {_q(states[i]['delta'])})"
        projection = self._projection or f"{aliases[0]}.*"
        where = f" WHERE {' AND '.join(self._filters)}" if self._filters else ""
        return (
            f'SELECT {projection}, {aliases[i]}.{_q(D_COL)} AS {_q(D_COL)} '
            f'FROM {self._from_clause(backing, aliases, spine_filter)}{where}'
        )

    def _full_join(self, spine_rel=None):
        """The output over the current source states + filter + projection (clean rows, no weight). With no
        ``spine_rel`` this is the comprehensive recompute (spine at its full current state); with one, the
        spine is backed by that relation instead — the spine-PK fast path joins only the *new* spine rows to
        the current dimensions."""
        con = self.pond.con
        aliases = self._aliases()
        backing = []
        for j, ref in enumerate([self.spine_ref] + [t[0] for t in self._joins]):
            v = unique_name("full")
            rel = spine_rel if (j == 0 and spine_rel is not None) else self.pond.read_table(ref)
            rel.create_view(v, replace=True)
            backing.append(v)
        projection = self._projection or f"{aliases[0]}.*"
        where = f" WHERE {' AND '.join(self._filters)}" if self._filters else ""
        return con.sql(f"SELECT {projection} FROM {self._from_clause(backing, aliases)}{where}")
