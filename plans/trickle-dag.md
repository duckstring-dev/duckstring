# Plan: Trickle builder v2 — a DAG of binary incremental joins

A follow-up to `plans/trickle-relational.md`, sequenced **after** the remaining incremental-aggregation work
(steps 5–6 there) — aggregation is orthogonal and unchanged by this reframing, and is the higher
*user-facing* value, so it lands first. This plan replaces the builder's **spine-centric** core with the
general DBSP formulation: a left-deep **DAG of binary incremental joins**, composed sequentially, with
intermediate results materialised **per-run** so sub-results are shared.

It does **not** add cross-run state. See `plans/trickle-dbsp.md` gap #2: maintained cross-run traces
(persistence) and indexed arrangements (sub-linear joins) remain out of scope — the former needs disk/warm
state, the latter a different execution substrate. This plan delivers the *other* half I'd conflated with
gap #2: **within-run intermediate sharing**, which needs no persistence.

## Why — what the current spine+telescoping core misses

The shipped builder (`trickle_builder.py`) maintains a join as a **star**: one spine `s0` + bare dimensions,
composed by the telescoping IVM sum (`_term`/`_state_views`). Two real limits:

1. **No intermediate sharing.** The telescoping sum is already linear (n terms, not 2ⁿ — it's the exact
   regrouping of the product-rule delta), but *each* term is evaluated as an independent multi-way join. A
   shared sub-join (e.g. `A⋈B` needed by several terms, or across a deep chain) is recomputed per term. The
   DAG model computes each node **once per run** and reuses it.
2. **Bushy trees are inexpressible.** `(A⋈B)⋈(C⋈D)` has no spine — `.join()` requires a *bare* dimension
   (the snowflake guard), and a composed handle joined as a dimension has its delta discarded. So a bushy
   shape forces a Pond/ripple split today. The DAG model has no privileged spine, so it falls out.

The unchanged-side property also becomes structural rather than hand-reasoned: in `δ(X⋈Y)`, a term whose
operand didn't change is `δ=∅` and simply isn't generated — no "is this the spine / a dim, key-filter the
big side" special-casing.

## The model

Represent the computation as a **DAG of operators** — sources, binary equi-joins (any `how`), filters,
projections — and maintain it by composing the **lifted incremental operator** stage by stage:

```
δ(X ⋈ Y) = δX ⋈ Y_old  ⊎  X_new ⋈ δY        (binary; an unchanged operand drops its term)
```

left-deep through the DAG, so a 4-way / bushy join is a sequence of binary IVM steps, **not** a flat n-term
sum. Each operator node is materialised once per run (see below) and reused everywhere it appears.

- **Subsumes the spine path.** Spine + dimensions + telescoping becomes the special case "left-deep DAG
  where every join shares the spine operand." The spine-PK append fast path and the `_spine_recompute`
  affected-key path are re-expressed as DAG behaviours (key pre-filter on a binary term; recompute a node
  from its inputs).
- **Subsumes all join types.** Each binary node carries its `how` (`inner`/`left`/`right`/`full`/`semi`/
  `anti`); `right`/`full` stop being "solo + comprehensive" — a binary node maintains its own incomparables
  (the match-count transitions) like any other, so a `(A ⟕ B)` node composes into a larger DAG.

### Per-run materialisation (the "in-memory, recomputed each run" point)

Materialise each DAG node's current state once per run (a temp relation), so:

- **Sharing** — a node used by multiple downstream terms is computed once, not per term.
- **Planner insulation** — a materialised node is an optimisation fence: each stage's plan stays small and
  the planner *can't* choose not to reuse it. (This is the mitigation for the risk below.)

This is **within-run** only — rebuilt each run from the (persisted) source mains; it is **not** a cross-run
trace and gives none of the "apply only the delta to last run's intermediate" win. That win is gap #2
(persistence) and stays deferred. A `.merge()` boundary remains the way to get a *persisted* trace.

## API generalisation: star → tree

`.join()` must accept a **composed** operand, not only a bare source, so `(A⋈B)⋈(C⋈D)` is expressible:

```python
ab = pond.trickle("a.t").join(pond.trickle("b.u"), on="k")          # a sub-DAG, not yet materialised
cd = pond.trickle("c.v").join(pond.trickle("d.w"), on="k")
(ab.join(cd, on="j").select(...).merge("out", pk=...))               # bushy join, composed incrementally
```

The snowflake guard (a dimension must be bare) is lifted — a dimension may itself be a join DAG. `.alias()`,
`.filter()`, `.select()` attach to nodes as today. The terminals (`.merge`/`.append`) and their semantics
(Z-set out, chainable handle) are unchanged.

## The flags — two orthogonal axes (and `p` stays)

There are **two independent** strategy axes, both surfaced on `.merge()`/`.append()`, both default `True`:

- **`ivm`** — *reuse already-computed parts*. `True` = compose deltas through the operator DAG (skip
  recomputing unchanged sub-results). `False` = re-evaluate joins, no reuse.
- **`key_filter`** — *bound the work to the affected slice*. `True` = trace filter conditions back from the
  terminal onto the source tables, so each source is restricted to the rows that can affect a changed output
  row (sound over-computation, close to minimal). `False` = operate on full sources.

They do **not** couple — `key_filter` prunes *what you read*, `ivm` decides *whether you reuse computation*
over it. The 2×2 maps onto the strategies the builder already has, plus one unused corner:

| | `key_filter=True` | `key_filter=False` |
|---|---|---|
| **`ivm=True`** | compose Δ through the DAG, each term pruned to affected keys (today's telescoping `_term`) | compose Δ, terms join full states (unused — Δ-composition without the `IN (δ)` pre-filter; for when that subquery confuses the planner) |
| **`ivm=False`** | re-evaluate only the affected key-slice and diff (today's `_spine_recompute` / affected-key recompute) | full recompute + diff vs the main (today's `_full_join` comprehensive) |

So the flags *name the existing axes* rather than bolting on escape valves — three quadrants already exist.

**`p` stays** (the per-source change-fraction threshold). It's the auto-heuristic for *when key-filtering
pays off*: if a source's delta touches more than `p` of its rows, the affected slice ≈ the whole table, so
filtering buys nothing and the build falls through to full recompute. Keeping that automatic is fine; the
`ivm`/`key_filter` flags are the manual overrides on top (`p=1.0` already = "don't check, always try the
incremental/filtered path").

## The de-risking gate: the planner prototype

The one unknown that could invalidate the direction: **DuckDB's planner on a deep DAG of nested views**
(IN-subquery key filters, `UNION ALL` of delta terms, reconstructed prior states) may pick a bad join order
or blow up planning past some depth, and lose to a flat recompute. **Before the rewrite**, prototype one
bushy/deep case and compare:

1. inline views (planner optimises the whole DAG),
2. materialised intermediates (temp-table fence per node),
3. full recompute (`ivm=False`).

The result decides the **default materialisation strategy** (inline vs always-fence vs threshold) and
whether the flags are convenience escape valves or load-bearing. Do not start the rewrite until this is run.

## Interactions

- **Comprehensive fallback** is unchanged in spirit (bootstrap / coverage-miss / changed-overwrite-Ripple →
  no delta exists → full recompute), now expressed as "a node with no usable delta forces its subtree
  comprehensive." `ivm=False` is the manual version of the same.
- **Append spine-PK fast path** survives as a DAG special case: when the output PK is the (single) spine's
  PK and conflicts are waived+unlogged, prune to new spine rows ⋈ current dims. Re-derive it on the DAG
  rather than the spine machinery.
- **Aggregation** (`plans/trickle-relational.md` §5) sits *on top* of the DAG output unchanged — the DAG
  produces the joined ΔO, the aggregate operator consumes it. Building aggregation first costs nothing here.
- **Determinism contract** (retractions cancel by full-row identity) is unchanged and still required.
- **Chained `.merge()`** still works and is now the explicit "persist this node as a cross-run trace" lever
  (vs the implicit per-run materialisation).

## Sequencing & effort

1. **(prereq) Incremental aggregation** — `plans/trickle-relational.md` steps 5–6. Orthogonal; lands first.
2. **Planner prototype** (the gate above). Small, decides the design.
3. **DAG IR + per-run materialisation** — the builder core rewrite; spine+telescoping becomes a special
   case. Largest piece.
4. **Composed-operand `.join()`** (star → tree; lift the snowflake guard) + bushy-tree tests incl.
   `(A⋈B)⋈(C⋈D)`.
5. **`ivm` / `key_filter` flags** (orthogonal; keep `p` as the key-filter payoff heuristic).
6. **Re-express** the append spine-PK fast path and the per-join-type incomparable maintenance on the DAG;
   migrate the existing trickle test suite (behaviour must be unchanged — this is an engine swap, not a
   surface change).

Effort: large (a core rewrite). But it's **internal** — the public surface (`.join/.filter/.select/.alias/
.merge/.append/.sql`) and correctness are unchanged, so it carries no backwards-compatibility risk; it can
land whenever, and the only cost of waiting is integration churn against a larger codebase.

## Open questions

- **Default materialisation**: inline views, always-fence (temp table per node), or fence-past-a-size — set
  by the prototype.
- **Node identity for sharing**: detect a repeated sub-DAG (so `A⋈B` used twice is materialised once) by
  structural hashing of the op graph, or require the dev to share it explicitly (one Python handle reused)?
  Lean: structural dedup within one build.
- **`p` granularity**: it's per-source and governs the `key_filter` payoff (large slice → fall through to
  full). Keep as-is; confirm it composes sensibly when a build mixes `key_filter`-worthwhile and
  `key_filter`-futile sources (per-source decision, not whole-build).
- **Bushy + outer**: a `full`/`right` node deep in a bushy DAG — does per-node incomparable maintenance
  compose cleanly through a downstream join? (Believed yes; confirm in the prototype.)
