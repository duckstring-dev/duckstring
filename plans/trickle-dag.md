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
  `anti`) and maintains its own output — including outer joins' incomparables — so `right`/`full` stop being
  "solo + comprehensive" and a `(A ⟕ B)` node composes into a larger DAG. See **Join semantics** below.

### Per-run materialisation (the "in-memory, recomputed each run" point)

Materialise each DAG node's current state once per run (a temp relation), so:

- **Sharing** — a node used by multiple downstream terms is computed once, not per term.
- **Planner insulation** — a materialised node is an optimisation fence: each stage's plan stays small and
  the planner *can't* choose not to reuse it. (This is the mitigation for the risk below.)

This is **within-run** only — rebuilt each run from the (persisted) source mains; it is **not** a cross-run
trace and gives none of the "apply only the delta to last run's intermediate" win. That win is gap #2
(persistence) and stays deferred. A `.merge()` boundary remains the way to get a *persisted* trace.

## Join semantics — all types incremental, in scope for v2

Every join type is maintained by **one rule**: the *affected-key recompute* (equivalently, match-count
incomparable maintenance). This is the v2 generalisation that **drops the current `right`/`full` "solo +
comprehensive" restriction** — in the DAG each binary join node maintains its own output incrementally,
**including the NULL-padded "incomparable" rows** that outer joins preserve, so a `(A ⟕ B)` node composes
into a larger DAG like any other.

**The rule.** For a binary equi-join `O = A ⋈ₖ B` of any `how`, with source deltas `δA`, `δB`:

- affected key set `K = πₖ(δA) ∪ πₖ(δB)` — the join-key *values* that changed on either side, and **only**
  those (evaluate match information for nothing else);
- recompute the join (of that `how`) restricted to `key ∈ K`, over both the **new** and the **old** states;
- emit `O_new|K (+1) ⊎ O_old|K (−1)`, consolidated.

Rows for any key ∉ K are provably unchanged, so they never appear in the delta. Re-evaluating a key's full
output old-vs-new *is* the match-count logic, just computed by re-evaluation rather than explicit counters —
either implementation is sound; re-evaluation is less bookkeeping. `key_filter` (above) is exactly the `IN
(K)` restriction; with it off, the same recompute runs unrestricted.

**Per type** (matched part = the inner join; an *incomparable* is a NULL-padded preserved row):

| `how` | output |
|---|---|
| `inner` | matched rows only |
| `left` | matched **+ A-side incomparables** — an A row with no B match → `(A, NULL)` |
| `right` | matched **+ B-side incomparables** → `(NULL, B)`; i.e. `left` with the operands swapped |
| `full` | matched **+ both sides' incomparables** |
| `semi` / `anti` | A rows that **have** / **lack** a B match (existence filters; A-grained output) |

**`full` is just `left` run on both sides.** It is *not* harder than `left` — it's the same incomparable
management applied symmetrically (A-side *and* B-side). What made it awkward in v1 was the spine-anchored
mechanism, which only ever enumerates one side's unmatched rows; a DAG node has no privileged spine, so both
sides fall out of the same recompute. `right` = `left` with operands swapped; `full` = `left` + `right`.

**The incomparable transitions** (what the re-evaluation reproduces; all bounded to `K`):

- *First match* — `δB` lifts a key's B-count `0 → >0`: retract the incomparable `−(A, NULL)` (and add the
  matched `+(A, B)`).
- *Last match* — `δB` drops it `>0 → 0`: insert the incomparable `+(A, NULL)` (and retract `−(A, B)`).
- *New unmatched* — `δA` adds a row whose key has B-count `0`: insert `+(A, NULL)`.
- (Symmetric on the A-side for `right`/`full`.)

**Status & migration from v1.** Today: `left`/`semi`/`anti` are incremental but **spine-anchored**
(`_spine_recompute`); `right`/`full` are restricted to a **solo** join and recompute **comprehensively**
(`_full_join` + diff). v2 makes **all six** incremental and composable in a bushy DAG via the rule above —
`right`/`full` lose both the solo restriction and the comprehensive fallback. The *only* comprehensive cases
that survive are the universal no-delta-available ones (bootstrap / coverage-miss / a changed overwrite
Ripple source), which force a full recompute for **every** join type, outer or not.

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

1. inline views (planner optimises the whole DAG) - preferred if proven to not be a footgun,
2. materialised intermediates (temp-table fence per node),
3. full recompute (`ivm=False`).

The result decides the **default materialisation strategy** (inline vs always-fence vs threshold) and
whether the flags are convenience escape valves or load-bearing. Do not start the rewrite until this is run on a sufficiently complex, large test case, and a more typical star schema.

### Gate result (run 2026-06-20 — DuckDB 1.4.3, fact=2M rows, dims=50k, ~20-key delta)

Prototypes: `/tmp/dag_planner_prototype.py` (single-term, star/bushy/chain) and `/tmp/dag_sharing_prototype.py`
(the multi-changed-source UNION-ALL telescoping sum, with reconstructed prior states — the case where fencing
*should* win via shared sub-results). Results (min/median ms, warm):

| case | inline | fence | full recompute |
|---|---|---|---|
| star (1 dim changed, 4-way) | **3.5** | 4.6 | 474 |
| bushy `(A⋈B)⋈(C⋈D)` | **3.1** | 4.1 | — |
| chain depth 4 / 8 / 12 | **0.9 / 1.7 / 2.5** | — | 6.5 / 7.6 / 8.1 |
| 6-way star, 2 dims changed | **12.6** | 22.0 | — |
| 6-way star, 4 dims changed | **23.4** | 33.4 | — |
| 6-way star, 6 dims changed | **29.8** | 42.2 | — |

**Decision: inline nested views, no per-node fence.** Findings:

1. **Inline always wins, including the sharing case.** Fencing (a `CREATE TEMP TABLE` per node) is a net
   *pessimization* at these scales — the key pre-filter restricts every term's spine to the ~20 changed keys
   *before* the join, so re-evaluating a shared sub-join inline is nearly free, while materialising shared
   prior states costs more than it saves. The plan's concern #1 (no intermediate sharing) is real in theory
   but does not pay off in practice for the key-filtered incremental path.
2. **No planner blow-up.** Deep nesting (chain depth 12) and `UNION ALL` of up to 6 telescoping terms with
   reconstructed prior states plan and execute fine; inline scales ~linearly with depth/terms.
3. **The key pre-filter is doing the heavy lifting** — it is what makes inline cheap, so `key_filter` is the
   load-bearing axis and `ivm` (reuse) is the secondary one. Full recompute is 100×+ slower (474ms vs 3.5ms),
   confirming the comprehensive fallback should stay a fallback.

Consequences for the rewrite: **per-run materialisation is NOT the default** (it stays available only as the
explicit `.merge()` persistence boundary, which also buys cross-run reuse). The DAG IR composes to **inline
SQL**; the `ivm`/`key_filter` flags are convenience escape valves over an inline default, not load-bearing
machinery. This deviates from §"Per-run materialisation" above — keep that section as the rejected
alternative.

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

1. **(prereq) Incremental aggregation** — `plans/trickle-relational.md` steps 5–6. Orthogonal; lands first. **✅ done.**
2. **Planner prototype** (the gate above). Small, decides the design. **✅ done** — see §"Gate result":
   inline wins, no per-node fence.
3. **DAG IR ~~+ per-run materialisation~~** — the builder core rewrite; spine+telescoping is gone, replaced
   by a DAG of `_Source`/`_Join` nodes maintained by the affected-key recompute, composed to **inline SQL**
   (the gate dropped per-run materialisation). **✅ done** (`trickle_builder.py`).
4. **Composed-operand `.join()`** (star → tree; lift the snowflake guard) + bushy-tree tests incl.
   `(A⋈B)⋈(C⋈D)`. **✅ done** — leaf-alias name resolution (`_resolve_col`/`_qualify`), bushy + incremental
   right/full tests in `tests/test_trickle.py`.
6. **Re-express** the append spine-PK fast path and the per-join-type incomparable maintenance on the DAG;
   migrate the existing trickle test suite (behaviour unchanged — an engine swap). **✅ done** (the spine-PK
   fast path is preserved over the DAG; all six `how` are incremental via the one recompute rule; the
   internal-method-coupled tests were migrated to the new IR; full suite green).

5. **`ivm` / `key_filter` flags** — surfaced on `.merge()`/`.append()` as manual strategy escapes (both
   default `True`). **✅ done.** `ivm=False` ignores deltas entirely and recomputes the whole output with
   plain full-table joins diffed vs the stored main (the escape when the delta machinery is counterproductive,
   short of raw `.sql()`; also disables the append spine-PK fast path). `key_filter=False` keeps the delta
   composition but drops the `IN (…)` pre-filter (joins full new/old states and diffs — for when the change
   is large enough to trip `p` anyway, so the filter buys nothing). `p` stays as the automatic key-filter
   payoff heuristic (a source over `p` reads `is_full` → comprehensive). Tests:
   `test_builder_ivm_false_forces_comprehensive`, `test_builder_key_filter_false_skips_in_filter`.

Effort: large (a core rewrite). But it's **internal** — the public surface (`.join/.filter/.select/.alias/
.merge/.append/.sql`) and correctness are unchanged, so it carries no backwards-compatibility risk; it can
land whenever, and the only cost of waiting is integration churn against a larger codebase.

## Open questions

- **Default materialisation** — *resolved:* **inline views** (the gate; no fence). Per-node `CREATE TEMP
  VIEW`, planner-inlined; `.merge()` is the only fence.
- **Node identity for sharing** — *moot under inline.* No within-run materialisation, so there is nothing to
  dedup; sharing is the planner's job over the inlined SQL. A repeated handle is recompiled to repeated SQL,
  which the planner can common-subexpression. Revisit only if a future fence mode lands.
- **`p` granularity** — *kept as-is:* per-source; a source over `p` reads `is_full`, which propagates up its
  subtree (so a mixed build decides per source, not whole-build).
- **Bushy + outer** — *resolved: yes.* A `full`/`right` node's incomparables are maintained by the same
  affected-key recompute and its `δO`/`O_new`/`O_old` feed a downstream join like any node. Covered by
  `test_builder_full_join_incremental_matches_comprehensive` and the bushy test.
