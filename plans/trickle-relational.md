# Plan: Trickle relational completeness — joins, aliases, the `.sql()` escape, Ibis interop, incremental aggregation

Extends `plans/trickle-dbsp.md` (the incremental-join core). That doc names the two highest-value next
steps: **(1) incremental aggregation** and **(2) indexed traces**. This plan does (1), plus rounds out the
builder's relational surface (all join types, source aliases) and pins the **`.sql()` escape hatch** as the
hard boundary between "incrementally maintained" and "comprehensive". It does **not** attempt (2) — a
Trickle boundary remains the in-philosophy substitute for a maintained arrangement.

## Guiding principle: a builder method ⟺ a Trickle-specific incremental side effect

The builder is **not** a general dataframe API. A method earns its place *only* if the engine handles it
incrementally (linear/bilinear over Z-sets, or a dedicated retraction-aware operator). Everything else —
window functions, `DISTINCT`, set ops, ordering, ad-hoc expressions — goes through **`.sql()`**, the
comprehensive escape hatch. **No convenience sugar that just wraps SQL without an incremental benefit.**

The incrementally-maintained op set, after this plan:

| Method | Operator | Linearity |
|---|---|---|
| `pond.trickle(ref)` | source | — |
| `.join(other, on, how=)` | equi-join, **all join types** | bilinear (inner) / recompute-diff (outer/semi/anti) |
| `.filter(pred)` | selection | linear |
| `.select(proj)` | projection (SQL string — **kept**, the Ibis expression API is the one big justified deviation) | linear |
| `.alias(name)` | naming (no compute) | — |
| `.aggregate(by, **metrics)` | grouped aggregation | dedicated incremental operators (distributive + algebraic) |
| `.merge(name, pk)` / `.append(name, …)` | Trickle write terminals | — |
| `.sql(query)` | **escape** → comprehensive from here | non-incremental by definition |

Introspection (not transforms, so exempt from the no-sugar rule): `.schema()` / `.to_ibis_schema()`.

---

## 1. `.alias()` — name a source (smallest, ship first)

Positional `s0`/`s1` couple `.select` correctness to `.join()` declaration order — and we *encourage*
reordering joins for cost (volatile-inner). `.alias()` decouples them (Ibis idiom).

- `pond.trickle(ref).alias("o")` sets this node's name. `.select`/`.filter` reference it (`"o.col"`)
  instead of `s0`. `s0`/`s1` stay as the fallback for unaliased sources → fully back-compatible.
- Dual use (matches Ibis): on a **source** the parent join uses the name; on the **self/output** it's the
  handle `.sql()` references (`.alias("x").sql("… FROM x")`).
- Implementation: store `self._alias`; in `_from_clause`, the SQL alias for a backing is its source's
  alias if set, else `s{j}`. The spine filter / term machinery is unaffected (it operates inside the
  aliased subquery on bare column names).
- **Fast-path interaction:** `_spine_pk_passthrough`'s regex keys on `s0.<col>`. Generalize it to the
  spine's alias, or aliasing the spine silently disables the spine-PK append optimization. (Detector stays
  conservative — match `<spine_alias>.<col>` only.)

Effort: small. Independently shippable.

---

## 2. Join types beyond `inner`

Today `.join()` is inner-only — a real gap (a `LEFT` fact-keeps-when-dim-missing is the most common ask).
Add `how=` ∈ {`inner`, `left`, `right`, `outer`, `semi`, `anti`} (`cross` out of scope).

### The general incremental strategy: affected-key recompute-diff

The bilinear telescoping sum is an *inner-join* optimization; NULL-padding (outer) and existence
(semi/anti) aren't bilinear. The general rule that covers **every** join type:

> The join output for a key `k` depends only on the rows with key `k` on each side. So:
> **affected keys `K` = the join-key values appearing in any changed source's delta**; recompute the join
> (of the requested `how`) restricted to `K` over both the **old** and **new** source states; emit
> `O_new|K (+1) ⊎ O_old|K (−1)`, consolidated.

Sound for inner/left/right/outer/semi/anti because rows outside `K` are provably unchanged. Old states are
the already-reconstructed `prior = consolidate(current ⊎ −delta)`; the key pre-filter (already built)
restricts both sides to `K`, so cost tracks the delta, not the table.

### Phasing

- **inner** — keep the shipped telescoping path (efficient, tested). Unchanged.
- **left (spine-preserved)** — highest value and a *natural* fit: every spine row yields exactly one
  output row (matched or NULL-padded), so it composes in the star exactly like inner with a `LEFT JOIN`,
  and it strengthens the spine-PK append fast path (a new spine row → always one output row). Implement
  via the recompute-diff over affected keys (a dim change flips affected spine rows between matched and
  NULL-padded).
- **right / full / semi / anti** — recompute-diff, single-join first; multi-way (star) outer is the hard
  tail (a dim change must recompute the *whole* multi-join for affected spine keys) — land after left.

### Cross-cutting

- **Determinism / retraction soundness** already hold: retractions cancel by full-row identity via
  `IS NOT DISTINCT FROM`, which is NULL-correct — so NULL-padded outer rows retract cleanly.
- **Comprehensive fallback** unchanged (any `is_full` source → recompute whole output, diff vs main).
- **`.select` must tolerate NULLs** from the unmatched side (the user's responsibility; document it).
- Decide: keep inner on telescoping + a parallel recompute-diff path for the rest, **or** unify all types
  on recompute-diff (simpler code, inner slightly less optimal). Recommendation: parallel paths now,
  revisit unification once outer is exercised.

Effort: medium (left), large (full/semi/anti multi-way).

---

## 3. `.sql()` — the comprehensive escape hatch

(Designed in discussion; recorded here.) `.sql(query)` collapses everything composed so far into **one
relation**, exposes it under the builder's alias, runs the raw query, and returns a builder in
**comprehensive mode**.

- **Breaks incremental _compute_** — after `.sql()` the data is a full materialised relation: no
  telescoping join, no key pre-filter, no spine-PK fast path. "Resorting to `.sql()` means no shortcuts."
- **Keeps incremental _output_** — the terminal `.merge()` still diffs the result against the prior main
  (full-row Z-set diff), so only changed rows hit the changelog. ("The win is the small delta out, not
  less compute in.")
- Input relation: a chained handle → `read_table(that_table)` (full current main); mid-builder → the
  comprehensive `_full_join()`. Either way one relation, aliased, fed to the query.
- Mechanics: introduces a genuine **comprehensive-mode** builder state (output is a full relation, not a
  Z-set). Either a small wrapper class whose `.merge()`/`.append()` just diff the relation, or a
  `self._materialised` field that `_compute`'s terminal special-cases. Post-`.sql()` ops (`.select`,
  `.filter`, another `.sql()`, terminal) are all comprehensive.

This is the `priced → revenue` ripple boundary brought inside one ripple — but see §5: once aggregation is
native, `.sql()` is for the genuinely non-incrementalizable tail, not for aggregates.

Effort: small–medium.

---

## 4. Ibis interop without an Ibis dependency

Goal: trivially hop into Ibis *if the user has it*, zero dep for those who don't.

- **`.schema()` → `dict[str, str]`** (DuckDB types) — the introspection primitive. Build the output
  relation lazily and read `con.sql(query).columns` / `.types` (no execution).
- **`.to_ibis_schema()` → `dict[str, str]`** — the same, with a small DuckDB→Ibis type map
  (`BIGINT→int64`, `VARCHAR→string`, `DOUBLE→float64`, `BOOLEAN→boolean`, `DATE→date`,
  `TIMESTAMP WITH TIME ZONE→timestamp('UTC')`, `DECIMAL(p,s)→decimal(p,s)`, …; raise on an unmapped type
  rather than guess). No `import ibis` — it returns a plain dict `ibis.table(schema, name=…)` accepts.
- **`.sql()` accepts a compiled query _or_ an Ibis expression** — duck-typed: a non-`str` argument is run
  through `ibis.to_sql(expr)` (imported lazily, only if passed an expr). So:
  ```python
  import ibis
  t = ibis.table(priced.to_ibis_schema(), name="pl")
  agg = t.group_by("product_id").aggregate(total=t.revenue.sum())
  priced.alias("pl").sql(agg).merge("revenue_by_product", pk="product_id")
  ```
  Still comprehensive (it's `.sql()`), but lets power users express the tail in Ibis instead of raw SQL.

Effort: small (`.schema`/`.to_ibis_schema`); the `.sql(expr)` duck-type is trivial.

---

## 5. Incremental aggregation — the headline

Replaces the comprehensive `revenue`-style downstream step with a **natively incremental** grouped
aggregate: only the groups a delta touches are recomputed.

### Surface

`.aggregate(by, **metrics)` (and `.group_by(by).aggregate(**metrics)` as the Ibis-shaped alias — same
operator). Metrics are **typed helper objects**, not SQL strings, because each carries its incremental
rule and the accumulators it needs:

```python
from duckstring import agg

(pond.trickle("orders.order_line").alias("o")
     .join(pond.trickle("catalog.product").alias("p"), on="product_id")
     .select("o.product_id, o.quantity, round(o.quantity * p.unit_price, 2) AS revenue")
     .aggregate(by="product_id",
                total_revenue=agg.sum("revenue"),
                units_sold=agg.sum("quantity"),
                order_count=agg.count(),
                avg_revenue=agg.mean("revenue"))
     .merge("revenue_by_product", pk="product_id"))   # pk defaults to `by`
```

The `by` columns are the output identity (pk defaults to them).

### Supported metrics

- **Distributive** (delta-only update):
  - `count()` — `+ Σ weight`.
  - `sum(x)` — `+ Σ weight·x`.
  - `min(x)` / `max(x)` — a `+1` extends the extreme cheaply; a `−1` of a row whose `x` equals the
    stored extreme triggers a **group rescan** (see below). This is the "MIN must know the runner-up"
    problem from the DBSP doc, solved by rescan-on-extreme-retraction (no maintained sorted structure —
    fine for micro-batch).
- **Algebraic** (derived from distributive accumulators):
  - `mean(x)` = `sum(x) / count`.
  - `var(x)` / `stddev(x)` = maintain `count`, `sum(x)`, `sum(x²)` → `var = sumsq/n − (sum/n)²` (population;
    `_sample=True` for `n−1`); `stddev = sqrt(var)`. (Document the numerical caveat of the sum-of-squares
    form.)

### State & maintenance

- **Accumulator state**: a registry-only companion `_duckstring_aggstate_{name}` (reserved prefix →
  `registry_tables` already hides it from publish), one row per group holding the raw accumulators
  (`count`, `sum_<col>`, `sumsq_<col>`, `min_<col>`, `max_<col>`) every requested metric needs. The
  published main holds only the **derived user columns** — keeping the "merge main is pure user columns"
  invariant intact, and internal accumulators out of the version contract.
- **Per-run flow**:
  1. Compose the input ΔO (the join delta) as today.
  2. `affected = DISTINCT by(ΔO)`.
  3. **Distributive update**: for affected groups, fold ΔO's weighted contributions into the stored
     accumulators (sum/count/sumsq are pure Z-set folds; idempotent under the same `f`).
  4. **min/max**: `+1` rows extend in-place; collect `rescan` groups (a `−1` retracted a row whose value ==
     the stored extreme). For those, recompute min/max from the group's **current membership** =
     `_full_join()` restricted to the rescan groups (key pre-filter on `by`). Cost bounded by rescan
     groups, not the table.
  5. Derive the output rows for affected groups; emit `new(+1) ⊎ old(−1)` and `apply_zset` into the output
     main + changelog (reusing the existing terminal). Only moved groups reach the changelog → small delta
     out (the whole point).
- **Comprehensive fallback**: input `is_full` (bootstrap / coverage-miss) → recompute all groups via a
  full `GROUP BY` over the current join output, rebuild the accumulator state, diff vs the prior main.
- **A group emptying out** (last member retracted) → its output row is retracted (a `−1`, no `+1`); drop
  its accumulator row.

### Placement & interactions

- `.aggregate()` is a transform producing a grouped delta; it must be followed by `.merge()` (the
  natural terminal; `pk` defaults to `by`). `.append()` after `.aggregate()` is nonsensical (aggregates
  update, not append) — raise a `BuildError`.
- After `.aggregate()`, further `.join()`/`.aggregate()` are out of the incremental op set → require
  `.sql()` or a downstream Trickle. (A second aggregation = a downstream node.)
- Spine-PK fast path doesn't apply (output is keyed by `by`, not the spine).

Effort: large. This is a new stateful operator. The distributive metrics (sum/count + mean) are the
80% and the cleaner first cut; min/max-with-rescan and var/stddev are the second cut.

---

## Sequencing (each independently shippable)

1. **`.alias()`** (+ fast-path regex generalization). Small, unblocks readable multi-joins.
2. **`.sql()` escape + comprehensive-mode terminal.** Small–medium; immediately collapses the
   `priced→revenue` split into one ripple even before native aggregation lands.
3. **`.schema()` / `.to_ibis_schema()` + `.sql(expr)`.** Small; rides on (2).
4. **`how="left"` join (spine-preserved).** Medium; the highest-value join gap.
5. **Incremental aggregation — distributive (sum/count/mean).** Large; the headline. Supersedes the
   `.sql()` aggregate for the common case with a genuinely incremental one.
6. **Aggregation — min/max (rescan) + var/stddev.** Large.
7. **`how=` right/full/semi/anti.** Medium–large; the long tail of join types.

After (1)–(3) the builder already matches the discussed ergonomics; (4)–(7) close the DBSP completeness
gaps in value order.

## Open questions

- **`.sql()` comprehensive-mode**: a wrapper class vs a `_materialised` flag on `TrickleBuilder`. Wrapper
  is cleaner conceptually; the flag avoids a second type the terminals must understand. Lean wrapper.
- **Inner join**: keep telescoping, or unify all `how` on recompute-diff for one code path? Keep
  telescoping until outer is exercised, then reassess.
- **`agg` metric helpers**: a submodule (`duckstring.agg.sum`) vs top-level (`duckstring.Sum`). Lean
  submodule — keeps the top-level namespace to the node decorators.
- **min/max rescan** when a Trickle source's retention has trimmed history below the needed membership →
  falls back to the comprehensive path for that group (coverage-miss semantics; reuse the floor check).
- **Sample vs population** default for `var`/`stddev` (Ibis defaults to sample). Match Ibis: sample
  default, `agg.var("x", how="pop")` for population.
