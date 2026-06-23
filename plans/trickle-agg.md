# Plan: Trickle aggregation breadth — provably-incremental metrics

Extend the `.aggregate(...)` operator (`duckstring/trickle/agg.py` specs + `trickle/io.py:apply_aggregate`)
so that the set of available aggregations covers the four flavours below, **without ever asking the user to
reason about whether a given aggregation is incrementally maintainable**. The guarantee is the design
principle: *if it's in the `agg.*` namespace and the builder accepts it on the terminal you used, it is
sound and incremental.* Everything that can't meet that bar is either a derivation over an accumulator we do
maintain, or it stays out of the namespace (→ `.sql()` comprehensive).

## The four flavours → three mechanisms

| Flavour | Algebraic structure | Mechanism | Allowed on |
|---|---|---|---|
| 1. Retractable, order-independent (sum, mean, var, cov, …) | commutative **group** (has an inverse) | additive accumulator vector; a retraction is `−x` | `.merge()` + `.append()` |
| 2. Non-retractable, order-independent (min, max, argmax, …) | commutative **semigroup** (no inverse) | accumulator + **rescan the group on a retraction** (the existing min/max path) | `.merge()` + `.append()` |
| 3. Discrete order-dependent (cumsum, ema, running_*) | sequential fold over rank | per-group **carried fold-state**, tail-extended | **`.append()` only** |
| 4. Continuous order-dependent (time-decayed ema) | sequential fold over a gap | same, the fold reads the order-column *value* | **`.append()` only** |

Mechanisms 3 and 4 are one implementation: a per-group state carried across runs, folded forward by this
run's new rows in order. They are incremental **iff** new rows only ever extend the *tail* of each group's
ordered sequence — which `.append()` (insert-only, no retraction) plus a **freshness-monotonic order column**
guarantee. That order column is declared with a new builder method **`.along(col)`** (§ "The `.along` axis").

## Numerical safety — the enabling decision (Phase 0)

Most of flavour 1 (variance, covariance, correlation, skewness, OLS, z-score) reduces to **central moments
and co-moments**. The shipped `var`/`stddev` forms them the naive way — `Σx² − (Σx)²/n` — and only *clamps*
the result to ≥0, which hides catastrophic cancellation rather than avoiding it (large `n`, large mean, tiny
variance → the subtraction loses all significant bits).

The fix, and the precondition for shipping the rest of flavour 1 with confidence, is to maintain **centred
moments via the parallel (Chan / Pébay) form**: per group keep `(n, Σx, M2)` with
`M2 = Σ(x − x̄)²`, updated by merging an *insert* partition in and a *delete* partition out:

```
merge-in  B into A:  n = nA+nB;  δ = x̄B − x̄A;  M2 = M2A + M2B + δ²·nA·nB/n
merge-out B from C:  nA = nC−nB; δ = x̄B − x̄A;  M2A = M2C − M2B − δ²·nA·nB/nC
```

This update is commutative, associative **and invertible** (so fully retractable) and is well-conditioned —
the running `M2` is never formed by a power-sum subtraction. The per-batch partition stats are taken over
the *delta only* (small), so their within-batch power sums don't cancel; the comprehensive rebuild uses
DuckDB's stable `var_pop` directly.

Residual risk — removing a large fraction of a group can still erode `M2` — is covered by two guards we
already have / nearly have:

- The builder's per-source change-fraction threshold `p` already routes a large delta to a clean
  comprehensive rebuild (a single stable pass).
- Add a **cumulative** guard later: rebuild a group's accumulator when its lifetime retraction count crosses
  a fraction of `n` (per-run `p` doesn't catch slow erosion from many small retractions). *(Deferred —
  noted here so it isn't forgotten.)*

`mean` and `sum` keep their own exact additive accumulators (`Σx`, non-NULL count) — best-conditioned for
their own purpose — and the `var`/`stddev` derivation reads `M2`/`n`. So the only stored-accumulator change
is **`_a_sumsq_{i}` → `_a_m2_{i}`** (the centred second moment).

## Phases

### Phase 0 — foundations (this change)

1. **`Metric` generalisation** (`agg.py`): add optional `col2` (second input — covariance/correlation/
   weighted variants) and an `ordered: bool` flag (so the builder can reject an order-dependent metric on
   `.merge()`). Frozen-dataclass fields with defaults; not yet consumed by the builder tuple — wired in
   Phase 1 with the first multi-column metric.
2. **Co-moment migration** (`trickle/io.py`): replace the per-additive-column `_a_sumsq_{i}` accumulator with
   the centred second moment `_a_m2_{i}`, maintained by the merge-in/merge-out form above. Touches
   `apply_aggregate` (`dacc` delta exprs, `macc` merge exprs, `acc_order`), `_agg_rebuild` (use stable
   `var_pop`), `_agg_derive` (`var`/`stddev` from `M2`/`n`). Format change only — Trickle is unreleased.
3. Tests: keep the existing var/stddev behaviour green; add a **numerical-stability** test (large offset,
   tiny variance) comparing the incremental result to DuckDB's native `var_samp`/`var_pop` over the
   reconstructed full set, across inserts **and** retractions (a merge that changes a value = `−old +new`).

*(Note: M2 is currently maintained for every additive column, including `sum`/`mean`-only ones. A later
optimisation can skip it where no `var`/`stddev`/moment metric reads the column.)*

### Phase 1 — flavour 1 breadth (retractable, order-independent) — **done**

Built on the Phase-0 moment subsystem; all O(δ) for small deltas, comprehensive rebuild beyond `p`.
Delivered:

- `weight_total(w)`, `weighted_sum(x, w)`, `weighted_average(x, w)` — additive `Σw`, `Σwx` (`_w_num`/`_w_den`).
- `covariance(x, y, how)`, `pearson_correlation(x, y)` — paired co-moment `Cxy` + `M2x`, `M2y` (the
  `_c_*` accumulator), maintained by the generalised parallel merge `_co2_merge` (a two-pass `dacc` for the
  partition co-moments; comprehensive rebuild via DuckDB `regr_sxx/syy/sxy`).
- `ols_slope(x, y)`, `ols_intercept(x, y)` — **two separate specs**, derived from `(n, Σx, Σy, M2x, Cxy)`.

`z_score`, `naive_bayes_update` are **recipes** over the above, not metrics (§ "Out of scope").

**Deferred from this phase** (kept out rather than shipped unsafe — the library's promise is that anything in
the namespace is sound):

- `skewness(x)` — the third moment `M3`'s *merge-out* (retraction) is numerically fragile; the safe form is
  rescan-on-retraction (extend the membership-rescan plumbing that min/max use). → a follow-up.
- `product(x)` — a running multiply/divide drifts and overflows; the safe form is `sign · exp(Σ log|x|)`
  with a zero-count (the additive `Σ log` *is* retractable). → a follow-up.
- `bit_xor(x)` — trivially safe (self-inverse) but niche; rolled forward to avoid scope creep here.

### Phase 2 — flavour 2 (extremes with a payload) — **done**

- `argmin(arg, by)`, `argmax(arg, by)` — carry a payload column (`_g_arg`) alongside the stored extreme
  (`_g_key`); reuse the retraction-rescan path (extended to recompute `arg_max`/`arg_min` over current
  membership). The rescan-family set is `RESCAN_KINDS` (drives `needs_current`).
- `bool_and`/`bool_or`/`bit_and`/`bit_or` — single reduced value (`_s_val`) via the same rescan-on-retraction
  mechanism (semigroups), combiner-parameterised (`_SG`).

`first_by`/`last_by` in the *arrival-order* sense are order-dependent → Phase 3 (the value-keyed "by" forms
are just `argmin`/`argmax`).

### Phase 3 — flavours 3 & 4 (order-dependent), `.append()`-only

- `.along(col)` axis declaration + validation: **required** for any order-dependent metric, **rejected on
  `.merge()`** with a clear error.
- A per-group **carried fold-state companion** (sibling of `_duckstring_agg_{name}`): `last`, `ema_old`,
  `cumsum_total`, `running_count`, `last_order_value`, `running_min`/`running_max`.
- Metrics: `cumsum`, `running_count`, `running_min`/`running_max`, `first`, `last`, `ema(alpha)` [discrete],
  `time_decayed_ema(t, lambda)` [continuous — reads the Δ of the order column].
- Two landmines to encode:
  - an **f-stamped replay guard** on the fold-state (fold this run's tail in only if not already applied at
    this `f`) — else a crash replay double-applies;
  - a **late-arrival policy** when a row violates the monotonic contract (order value below a group's
    processed high-water mark): document the contract as a precondition (like the determinism contract), and
    optionally detect-and-divert to the existing `__droplog`.

### Phase 4 — later / optional

- Approximate holistic metrics as **explicitly-approximate** specs: `approx_count_distinct` (HyperLogLog),
  `approx_quantile` (t-digest) — both mergeable sketches, so they fit the additive mechanism.
- A general **custom-fold / `.scan()`** primitive for power users (the escape hatch for order-dependent work
  not worth a first-class metric).

## The `.along` axis

A declared **freshness-monotonic stream axis** — conceptually distinct from a generic `order_by` (it's a
precondition, not a sort) and not required to be a key (flavours 3/4 don't need uniqueness):

```python
(pond.trickle("orders")
     .along("event_time")
     .group_by("product")
     .aggregate(ma=agg.time_decayed_ema("price", t="event_time", lam=0.1))
     .append("ema_by_product"))
```

## Out of scope (stays `.sql()` / recipes)

- **Exact holistic** — `median`, `percentile`, `mode`, exact `count_distinct`: not maintainable without
  sketches (already deferred in CLAUDE.md).
- **`z_score`** — a *per-row* enrichment `(x−μ)/σ` against group stats, i.e. a join-back, not a reduction. A
  documented recipe (aggregate μ,σ, then join), not a metric.
- **`naive_bayes_update`** — *per-class moments over each feature*: `group_by(class).aggregate(mean/var per
  feature)`. A recipe over Phase 1, demonstrating the small accumulator set composes.
- **ML model-state aggs** — `incremental_pca`, `online_kmeans`, `incremental_permutation_importance`: emit
  vector/matrix model state, order/init-sensitive, not SQL aggregations. Defer to the Phase-4 custom fold or
  user `.sql()`.
- **`convolution_1d`** — a bounded FIFO per group; a genuine flavour-3 fit but niche. Revisit on demand.
- **`string_agg`/`list_agg`/`array_agg`** — unbounded per-group state growth. Avoid (or gate behind
  retention if ever added).
