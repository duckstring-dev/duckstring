"""The ``pond.trickle(...)`` builder — optional sugar over the partial-merge helpers (see
``plans/trickle.md``).

A fluent builder that records a **tiny op graph** (``Source`` / ``Join`` / ``Filter`` / ``Project``) over
Trickle sources — its own minimal IR, **not** a general transform DSL. ``.merge(name)`` walks the graph:
``read_delta`` each source, propagate the affected spine keys along every recorded join edge
(:meth:`Pond.keys_joining`), recompute just the affected slice from the full sources, and write it via
``merge_table(comprehensive=False, deletes=…)``.

Why prefer it over hand-composed :meth:`Pond.keys_joining` for supported shapes: because it sees the
**whole** graph it can't *forget* a join edge the way a hand-rolled partial merge can — so it can't
silently under-merge. That safety, not just the terser API, is the point.

**Closed op set; hard error outside it.** Anything the op set can't express (a non-equi/self/cross join,
a window, ``having``, a non-PK join key, a snowflake chain through an intermediate) **raises at build
time** — it never silently degrades to a full refresh (a hidden performance cliff). The escape hatch is
a *downstream* Ripple/Trickle doing the gnarly part on the builder's output.
"""

from __future__ import annotations


class BuildError(ValueError):
    """The builder was asked for an op outside its closed set (raised at build time, never degraded)."""


def _cols(on) -> tuple[str, ...]:
    return (on,) if isinstance(on, str) else tuple(on)


class TrickleBuilder:
    """One node of the build graph. ``pond.trickle(ref)`` starts a graph rooted at the **spine** source
    (the one that owns the output PK); :meth:`join` attaches a dimension directly to the spine."""

    def __init__(self, pond, spine_ref: str) -> None:
        from .trickle_io import load_sidecar

        self.pond = pond
        self.spine_ref = spine_ref
        source_pond, table = spine_ref.split(".", 1)
        meta = load_sidecar(pond._source_data_dir(source_pond)).get(table, {})
        self.spine_pk = tuple(meta.get("pk", ()))
        if not self.spine_pk:
            raise BuildError(
                f"pond.trickle('{spine_ref}'): source is not a Trickle (no declared primary key). The "
                f"builder and the delta helpers need Trickle sources; to consume an overwrite Ripple, read "
                f"it with pond.read_table(...) and write a comprehensive pond.merge_table(...) instead"
            )
        self._joins: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []  # (dim_ref, dim_pk, on)
        self._filters: list[str] = []
        self._projection: str | None = None

    def join(self, dimension: "TrickleBuilder", *, on) -> "TrickleBuilder":
        """Equi-join a **dimension** (another single-source ``pond.trickle(...)``) directly to the spine.
        ``on`` equi-joins spine column(s) to the dimension's **full PK** (so delete propagation is sound —
        a delete tombstone carries only the PK). Build-time errors: a dimension that itself has joins (a
        snowflake — cap at direct edges; do deeper hops in a downstream ripple), or an ``on`` whose arity
        doesn't match the dimension's PK (a non-PK join key)."""
        if not isinstance(dimension, TrickleBuilder):
            raise BuildError("join() takes another pond.trickle(...) source as the dimension")
        if dimension._joins or dimension._filters or dimension._projection is not None:
            raise BuildError(
                f"join('{dimension.spine_ref}'): a dimension must be a bare source — a snowflake/transitive "
                f"chain isn't in the builder's op set; do the deeper hop in a downstream ripple"
            )
        on_cols = _cols(on)
        if len(on_cols) != len(dimension.spine_pk):
            raise BuildError(
                f"join('{dimension.spine_ref}', on={on_cols}): 'on' has {len(on_cols)} column(s) but the "
                f"dimension PK has {len(dimension.spine_pk)} — 'on' must equi-join to the dimension's full PK"
            )
        self._joins.append((dimension.spine_ref, dimension.spine_pk, on_cols))
        return self

    def filter(self, predicate: str) -> "TrickleBuilder":
        """Restrict the recomputed slice with a SQL boolean ``predicate`` (over the joined sources)."""
        self._filters.append(predicate)
        return self

    def select(self, projection: str) -> "TrickleBuilder":
        """The output column list (a SQL select list). Required when the graph has joins; it must include
        the spine PK (the merge identity). Spine columns are addressable as ``s0.*`` / ``s0."col"`` and
        the i-th joined dimension as ``s{i+1}``."""
        self._projection = projection
        return self

    def merge(self, name: str, *, pk=None, retain_t=None, retain_n=None) -> None:
        """Execute the incremental merge: affected spine keys → recompute that slice → ``merge_table(
        comprehensive=False, deletes=…)``. ``pk`` defaults to the spine's PK (the output identity)."""
        pond = self.pond
        out_pk = self.pond._resolve_pk(pk) if pk is not None else self.spine_pk

        # 1. Affected output (spine) keys: the spine's own changed keys, plus — per join edge — the spine
        #    keys a dimension change ripples to. Seeing every edge here is what makes this safe.
        spine_delta = pond.read_delta(self.spine_ref)
        affected = spine_delta.keys()
        for dim_ref, _dim_pk, on_cols in self._joins:
            dim_delta = pond.read_delta(dim_ref)
            affected = affected.union(pond.keys_joining(self.spine_ref, dim_delta, on=on_cols))

        # 2. Recompute just the affected slice from the full sources.
        recomputed = self._recompute(affected)

        # 3. Apply: upserts = the recomputed rows; deletes = affected keys that fell out of the recompute
        #    (a spine key whose row no longer exists — incl. source-deleted keys, folded in by .keys()).
        pond.merge_table(
            name, recomputed, comprehensive=False, deletes=affected.dropped(recomputed),
            pk=out_pk, retain_t=retain_t, retain_n=retain_n,
        )

    def _recompute(self, affected):
        if self._joins and self._projection is None:
            raise BuildError(
                f"pond.trickle('{self.spine_ref}').join(...): a joined graph needs .select(...) to name the "
                f"output columns (and include the spine PK)"
            )
        con = self.pond.con
        # Register each full source under a unique alias view; restrict to the affected spine keys.
        s0 = "_duckstring_tb_s0"
        self.pond.read_table(self.spine_ref).create_view(s0, replace=True)
        affected.create_view("_duckstring_tb_affected")
        froms = [f'"{s0}" s0']
        for i, (dim_ref, dim_pk, on_cols) in enumerate(self._joins):
            alias = f"_duckstring_tb_s{i + 1}"
            self.pond.read_table(dim_ref).create_view(alias, replace=True)
            cond = " AND ".join(
                f's0."{sc}" = s{i + 1}."{dc}"' for sc, dc in zip(on_cols, dim_pk, strict=True)
            )
            froms.append(f'JOIN "{alias}" s{i + 1} ON {cond}')
        key_cond = " AND ".join(f's0."{c}" = ak."{c}"' for c in self.spine_pk)
        froms.append(f'JOIN "_duckstring_tb_affected" ak ON {key_cond}')
        projection = self._projection or "s0.*"
        where = f" WHERE {' AND '.join(self._filters)}" if self._filters else ""
        return con.sql(f"SELECT {projection} FROM {' '.join(froms)}{where}")
