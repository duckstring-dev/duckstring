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

from .trickle_io import D_COL, _q, normalize_pk, unique_name


class BuildError(ValueError):
    """The builder was misconfigured (a missing ``.pk()``/``.select()``, a malformed join)."""


def _cols(on) -> tuple[str, ...]:
    return (on,) if isinstance(on, str) else tuple(on)


def _join_pairs(on) -> list[tuple[str, str]]:
    """Normalise ``on`` to ``[(spine_col, dim_col), …]``. A str/list names columns shared by both sides;
    a dict maps spine columns to dim columns (when the names differ)."""
    if isinstance(on, dict):
        return [(s, d) for s, d in on.items()]
    return [(c, c) for c in _cols(on)]


class TrickleBuilder:
    """One node of the build graph. ``pond.trickle(ref)`` starts a graph rooted at the **spine** source;
    :meth:`join` attaches a dimension directly to the spine."""

    def __init__(self, pond, spine_ref: str, *, p: float = 0.3) -> None:
        self.pond = pond
        self.spine_ref = spine_ref
        self.p = p  # this source's change-fraction threshold (see Pond.trickle)
        self._joins: list[tuple[str, list[tuple[str, str]], float]] = []  # (dim_ref, pairs, p)
        self._filters: list[str] = []
        self._projection: str | None = None

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

    def merge(self, name: str, *, pk, retain_t=None, retain_n=None) -> None:
        """Execute: compose ΔO from the changed sources' Z-sets (or recompute comprehensively) and apply it
        to the output Trickle ``name``. ``pk`` (**required**) is the output identity / merge key — it must be
        genuinely unique in the output (a many-to-many join that fans out past it corrupts the keyed main)."""
        pond = self.pond
        out_pk = normalize_pk(pk)
        if not out_pk:
            raise BuildError(f"pond.trickle('{self.spine_ref}')...merge('{name}'): pass the output key, merge(pk=...)")
        if self._joins and self._projection is None:
            raise BuildError(
                f"pond.trickle('{self.spine_ref}').join(...): a joined graph needs .select(...) to name the "
                f"output columns (and include the PK)"
            )

        refs = [self.spine_ref] + [dim_ref for dim_ref, _pairs, _p in self._joins]
        ps = [self.p] + [p for _dim_ref, _pairs, p in self._joins]
        deltas = [pond.read_delta(r) for r in refs]

        # Comprehensive fallback: any source can't supply a clean delta (full read), or changed more than
        # its threshold p. Recompute the whole output and diff it against the materialised prior output.
        over = any(self._over_threshold(r, d, p) for r, d, p in zip(refs, deltas, ps, strict=True))
        if any(d.is_full for d in deltas) or over:
            o_prime = self._full_join()
            cols = list(o_prime.columns)
            missing = [c for c in out_pk if c not in cols]
            if missing:
                raise BuildError(f".select(...) must include the PK column(s) {missing}")
            pond.merge_table(name, o_prime, pk=out_pk, retain_t=retain_t, retain_n=retain_n)
            return

        # Incremental: per source build current/prior/delta views; sum one join term per changed source.
        states = [self._state_views(r, d) for r, d in zip(refs, deltas, strict=True)]
        terms = [self._term(i, states) for i, st in enumerate(states) if st["changed"]]
        if not terms:
            return  # nothing changed (and no full read) → output is unchanged
        unioned = pond.con.sql(" UNION ALL BY NAME ".join(f"({t})" for t in terms))
        cols = [c for c in unioned.columns if c != D_COL]
        missing = [c for c in out_pk if c not in cols]
        if missing:
            raise BuildError(f".select(...) must include the PK column(s) {missing}")
        pond.apply_zset(name, unioned, pk=out_pk, retain_t=retain_t, retain_n=retain_n)

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

    def _full_join(self):
        """The whole output over the full current source states + filter + projection — the comprehensive
        recompute (clean rows, no weight)."""
        con = self.pond.con
        backing = []
        for ref in [self.spine_ref] + [dim_ref for dim_ref, _pairs, _p in self._joins]:
            v = unique_name("full")
            self.pond.read_table(ref).create_view(v, replace=True)
            backing.append(v)
        projection = self._projection or "s0.*"
        where = f" WHERE {' AND '.join(self._filters)}" if self._filters else ""
        return con.sql(f"SELECT {projection} FROM {self._from_clause(backing)}{where}")
