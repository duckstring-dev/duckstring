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


# A select item that is a verbatim spine pass-through: ``s0.col`` / ``s0."col"`` / ``s0.col AS alias``.
# group(2) = spine column, group(4) = alias (if any). Used to detect the spine-PK append fast path.
_S0_PASSTHROUGH = re.compile(r'^s0\.("?)(\w+)\1(?:\s+as\s+("?)(\w+)\3)?$', re.IGNORECASE)


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
        self._joins: list[tuple[str, list[tuple[str, str]], float]] = []  # (dim_ref, pairs, p)
        self._filters: list[str] = []
        self._projection: str | None = None
        self._spine_delta = _spine_delta  # set by an upstream .merge() → chained in-run operand

    def join(self, dimension: "TrickleBuilder", *, on) -> "TrickleBuilder":
        """Equi-join a **dimension** (another bare ``pond.trickle(...)``) directly to the spine on ``on``
        (any column(s); see :func:`_join_pairs`). The dimension must be a bare source — a snowflake/chain
        isn't in the op set (do the deeper hop in a downstream Trickle)."""
        if not isinstance(dimension, TrickleBuilder):
            raise BuildError("join() takes another pond.trickle(...) source as the dimension")
        if dimension._joins or dimension._filters or dimension._projection is not None:
            raise BuildError(
                f"join('{dimension.spine_ref}'): a dimension must be a bare source — a snowflake/transitive "
                f"chain isn't in the builder's op set; do the deeper hop in a downstream Trickle"
            )
        self._joins.append((dimension.spine_ref, _join_pairs(on), dimension.p))
        return self

    def filter(self, predicate: str) -> "TrickleBuilder":
        """Restrict the output with a SQL boolean ``predicate`` (over the joined sources, ``s0``/``s1``/…)."""
        self._filters.append(predicate)
        return self

    def select(self, projection: str) -> "TrickleBuilder":
        """The output column list (a SQL select list). Required when the graph has joins; it must include
        the output PK. The spine is ``s0`` and the i-th joined dimension is ``s{i+1}`` (``s0.*``, ``s1."col"``)."""
        self._projection = projection
        return self

    def merge(self, name: str, *, pk, retain_t=None, retain_n=None) -> "TrickleBuilder":
        """Execute: compose ΔO from the changed sources' Z-sets (or recompute comprehensively) and apply it
        to the output **merge** Trickle ``name`` (clean main + Z-set changelog). ``pk`` (**required**) is the
        output identity / merge key — it must be genuinely unique in the output (a many-to-many join that
        fans out past it corrupts the keyed main).

        Returns a :class:`TrickleBuilder` rooted at the just-materialised ``name``, so joins can be chained
        through intermediate materialisations **in one Ripple** —
        ``a.join(b).merge("ab", pk=…).join(c).merge("abc", pk=…)``. Each ``.merge()`` stores its output's
        trace (registry main + changelog), so a later run that changes only ``c`` reuses the stored ``ab``
        instead of recomputing ``a⋈b`` — the same win as splitting into a downstream Trickle, without the
        boilerplate (and without the parallelism a separate Ripple under a Wave would allow — a chain is
        explicitly sequential). The returned handle carries ``ab``'s in-run delta (read from the registry),
        so the downstream join composes without a round-trip through the not-yet-published data plane."""
        pond = self.pond
        out_pk = normalize_pk(pk)
        if not out_pk:
            raise BuildError(f"pond.trickle('{self.spine_ref}')...merge('{name}'): pass the output key, merge(pk=...)")
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
        out_pk = normalize_pk(pk)
        from . import trickle_io as trickle

        spine_delta = self._spine_delta if self._spine_delta is not None else pond.read_delta(self.spine_ref)
        if self._joins and not fail_on_conflict and not log_drops and self._spine_pk_passthrough(out_pk, spine_delta.pk):
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
        proj = {}
        for item in _select_items(self._projection):
            m = _S0_PASSTHROUGH.match(item)
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
        refs = [self.spine_ref] + [dim_ref for dim_ref, _pairs, _p in self._joins]
        ps = [self.p] + [p for _dim_ref, _pairs, p in self._joins]
        # The spine of a chained builder is a just-materialised in-run Trickle: use its known delta, not the
        # (unpublished) data-plane read. Dimensions are always bare sources read the normal way.
        if spine_delta is None:
            spine_delta = self._spine_delta if self._spine_delta is not None else pond.read_delta(self.spine_ref)
        deltas = [spine_delta] + [pond.read_delta(r) for r in refs[1:]]

        over = any(self._over_threshold(r, d, p) for r, d, p in zip(refs, deltas, ps, strict=True))
        if any(d.is_full for d in deltas) or over:
            o_prime = self._full_join()
            self._require_pk(out_pk, o_prime.columns)
            return "comprehensive", o_prime
        states = [self._state_views(r, d) for r, d in zip(refs, deltas, strict=True)]
        terms = [self._term(i, states) for i, st in enumerate(states) if st["changed"]]
        if not terms:
            return "empty", None
        unioned = pond.con.sql(" UNION ALL BY NAME ".join(f"({t})" for t in terms))
        self._require_pk(out_pk, [c for c in unioned.columns if c != D_COL])
        return "incremental", unioned

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

    def _from_clause(self, backing: list[str], spine_filter: str | None = None) -> str:
        """The ``FROM spine JOIN dim …`` clause, each alias ``s{j}`` backed by ``backing[j]`` (a view).
        ``spine_filter`` (a SQL predicate over the spine's own columns) restricts ``s0`` *before* the join
        — the key pre-filter (see :meth:`_term`)."""
        s0 = (f'(SELECT * FROM {_q(backing[0])} WHERE {spine_filter})' if spine_filter else _q(backing[0]))
        parts = [f'{s0} s0']
        for d, (_dim_ref, pairs, _p) in enumerate(self._joins):
            alias = f"s{d + 1}"
            cond = " AND ".join(f's0.{_q(sc)} = {alias}.{_q(dc)}' for sc, dc in pairs)
            parts.append(f'JOIN {_q(backing[d + 1])} {alias} ON {cond}')
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
            _dim_ref, pairs, _p = self._joins[i - 1]
            sc = ", ".join(_q(s) for s, _d in pairs)
            dc = ", ".join(_q(d) for _s, d in pairs)
            spine_filter = f"({sc}) IN (SELECT {dc} FROM {_q(states[i]['delta'])})"
        projection = self._projection or "s0.*"
        where = f" WHERE {' AND '.join(self._filters)}" if self._filters else ""
        return (
            f'SELECT {projection}, s{i}.{_q(D_COL)} AS {_q(D_COL)} '
            f'FROM {self._from_clause(backing, spine_filter)}{where}'
        )

    def _full_join(self, spine_rel=None):
        """The output over the current source states + filter + projection (clean rows, no weight). With no
        ``spine_rel`` this is the comprehensive recompute (spine at its full current state); with one, the
        spine is backed by that relation instead — the spine-PK fast path joins only the *new* spine rows to
        the current dimensions."""
        con = self.pond.con
        backing = []
        for j, ref in enumerate([self.spine_ref] + [dim_ref for dim_ref, _pairs, _p in self._joins]):
            v = unique_name("full")
            rel = spine_rel if (j == 0 and spine_rel is not None) else self.pond.read_table(ref)
            rel.create_view(v, replace=True)
            backing.append(v)
        projection = self._projection or "s0.*"
        where = f" WHERE {' AND '.join(self._filters)}" if self._filters else ""
        return con.sql(f"SELECT {projection} FROM {self._from_clause(backing)}{where}")
