# Plan: the builder's join delta, per term

Replaces the join rule in `plans/trickle-dag.md` ("affected-key recompute"). The DAG, the inline-SQL decision,
the comprehensive fallback and every terminal stay as they are; only how a join node computes its delta
changes.

## Problem

`_join_delta` computes `δ(L ⋈ R)` as `(L_new|K ⋈ R_new|K) − (L_old|K ⋈ R_old|K)`, consolidated, with one key
set `K = keys(δL) ∪ keys(δR)` restricting both sides. It is correct for every `how`, but it does work in
proportion to *all* rows under `K` on *both* sides, most of which cancel:

- A fact batch touches many keys. 25k new orders over 100k products hit about 22k products, so `K` is 22% of
  the catalogue and pulls 22% of all past orders into both the new and the old join, where they cancel.
- The old states are rebuilt in full: `_reconstruct_old` groups every row of each changed source
  (`current ⊎ −delta`, `HAVING SUM > 0`) before anything restricts it.
- Every join node consolidates its delta, and the terminal consolidates again.

The `plans/trickle-dag.md` gate (2026-06-20) measured this rule with deltas of about 20 keys, where `K` stays
tiny on both sides, so it never saw the shape above.

### Measurements (2026-10-17, DuckDB 1.5.6, in memory, `bench/trickle_join/`)

`priced` at 10× the demo: 15M orders, 1M products, 1,000 products repriced per run, one incremental run.

| | batch 10k | batch 250k |
|---|---|---|
| builder today (`K` restriction) | 263 ms | 462 ms |
| builder today, `key_filter=False` | 754 ms | 779 ms |
| builder `ivm=False` (recompute + diff) | 551 ms | 566 ms |
| hand-written per-term delta | 52 ms | 70 ms |

All four write the same changelog (39,522 and 279,522 rows). The breakdown at 250k: rebuilding the old
`orders` state costs 140 ms over all 15M rows; the `K` join produces 6.9M rows that consolidate to 280k. The
per-term delta produces 280,018 rows before consolidation (the only cancelling pairs are new orders on
repriced products), and consolidating it costs 1 to 6 ms.

## The rule

Write `L' = L + δL` (the current state is the prior state plus the change, as Z-sets).

### Inner: three terms, no prior state

```
δ(L ⋈ R) = δL ⋈ R'  +  L' ⋈ δR  −  δL ⋈ δR
```

Check: `δL(R + δR) + (L + δL)δR − δL δR = δL R + L δR + δL δR`, which is `L'R' − LR`. The weight of a term
row is the product of its operands' weights (a current state row weighs 1). A term whose delta is empty is
not generated, so a run where only `R` changed is the single term `L' ⋈ δR`.

The two-term forms (`δL ⋈ R' + L ⋈ δR`, or its mirror) need one prior state. Writing the prior state as
`L' − δL` and expanding gives the three terms above, which need none, so there's no orientation to choose and
nothing to rebuild.

### Semi, anti and the outer joins' unmatched rows: existence flips

These aren't bilinear: whether an `L` row survives depends on whether *any* `R` row has its key. Let
`E(k) = [R has a row with key k]`, before (`E`) and after (`E'`). Only a key in `δR` can change `E`, so the
**flip set** is

```
F = { k ∈ keys(δR) : E(k) ≠ E'(k) },   s(k) = E'(k) − E(k) ∈ {+1, −1}
```

computed from counts, never from a rebuilt state: `n'(k)` = rows of `R'` with key `k` (`R'` restricted to
`keys(δR)`), `n(k) = n'(k) − Σ δR weights at k`, and `k ∈ F` when `n'(k) > 0` differs from `n(k) > 0`. A key
with a NULL component never matches, so it is never in `F`.

With `L ⋉ R` the rows of `L` that have a match and `L ▷ R` the rows that don't:

```
δ(L ⋉ R) = δL ⋉ R'  +  s · L'|F  −  s · δL|F
δ(L ▷ R) = δL ▷ R'  −  s · L'|F  +  s · δL|F
```

(From `δ(L ⋉ R) = δL·E' + L·(E' − E)` with `L = L' − δL`; the anti join is `L` minus the semi join.)

### Per `how`

| `how` | delta |
|---|---|
| `inner` | the three inner terms |
| `left` | inner terms ⊎ `δ(L ▷ R)` padded with NULL `R` columns |
| `right` | inner terms ⊎ `δ(R ▷ L)` padded with NULL `L` columns |
| `full` | inner terms ⊎ both padded anti deltas |
| `semi` | `δ(L ⋉ R)` |
| `anti` | `δ(L ▷ R)` |

These are exact for every `how`, with multisets, deletes, updates (a −1 and a +1) and NULL keys, so none
falls back to the old rule; the comprehensive path is still taken only for a bootstrap, a coverage miss, a
changed overwrite source or a source over `p`. Padding casts NULL to the other side's column types.

### Key pushdown into subtrees

A term `X' ⋈ δY` (and `X'|F`) restricts `X'` by the join itself: the hash table is built on the small side
and `X'` is probed once. For a leaf `X`, an extra `IN (keys)` filter only adds a probe (measured +1 to 2 ms),
so none is added. When `X` is itself a join, DuckDB reorders an all-inner subtree on its own, but can't move a
join below an outer join, so `(O ⟕ P) ⋈ δC` scans all of `O` (60 ms against 16 ms with the keys pushed into
`O`, at 15M rows; for `(O ⋈ P) ⋈ δC` it was 15 ms against 13 ms).

So for a subtree operand, the keys are pushed down to the leaf that owns each key column. Every use of the
filtered subtree joins it on that key by equality, so a row whose key side is NULL-padded never contributes.
That makes the push valid on either side of any outer join: filtering a null-supplying side turns the rows it
removes into padded rows, which then fail the equality. Rather than produce them (one per row of the
preserved side), each outer join on the way down is tightened to the join without padding on the key's side:
`left` with the key on its right becomes `inner`, `right` with the key on its left becomes `inner`, `full`
becomes `left` or `right` (or `inner` when keys sit on both sides). The right side of a semi or anti join
never reaches the output, so a key can't come from there. A key spanning several leaves gets one filter per
column (each a superset; the join stays exact). `key_filter=False` turns pushdown off, which keeps the flag's
documented meaning.

Measured at 15M orders, `(O ⟕ P) ⋈ δCat` on `P.cat_id` (5 categories changed): the whole subtree 58 ms,
filtered without tightening 9 ms, filtered and tightened 5 ms. A bushy all-inner `(O ⋈ P) ⋈ (δC ⋈ G)` was
13 ms either way, and a chain `((O ⟕ P) ⋈ C) ⋈ δC` keyed in `O` 66 ms against 18 ms.

### Consolidation

A delta is consolidated once, at the root of the DAG, before the output pipeline. That's the contract the
terminals have today (`apply_aggregate`'s partitions, `.accumulate().append()` raising on any −1) and it
removes the three-term form's few cancelling pairs. Intermediate join deltas stay unconsolidated: every rule
above is linear in them or only sums their weights.

Deferring consolidation into the hot to warm fold was considered and rejected. Readers already consolidate,
so it would be correct, but every reader would then pay for it until the fold (each consumer's `read_delta`,
every rebuild of the current state, the next join), and a projection that drops the changed column would no
longer cancel to "no change", so the no-change skip would stop firing downstream. With per-term deltas the
write-time cost is 1 to 6 ms.

## Build

1. `packages/trickle/src/trickle/builder.py`: `_NodeState` loses `old`; `_compile_source` stops rebuilding the
   prior state (`_reconstruct_old`, `_W` go); `_join_delta` emits the terms above per `how`; `_affected_keys`
   and `_restricted_join` are replaced by the flip-set view and a pushdown helper over the compiled node
   currents; `_compute` consolidates the root delta. The module docstring states the new rule.
2. Tests (`packages/trickle/tests/`): a randomised differential test (every `how`, chains and bushy shapes,
   duplicate rows, NULL keys, inserts, updates and deletes on both sides over several runs, each checked
   against a full recompute), plus the existing join, builder, aggregate, capture and offer suites, and
   Duckstring's Trickle e2es (`test_trickle_chain_runs_end_to_end`, TPC-DS).
3. `bench/trickle_join/`: the measurement scripts and their results, re-run after the change.
4. Docs: the `key_filter` row in `docs/docs/reference/python/trickle_builder.md` and the docstrings it
   matches, the `p` paragraph in `docs/docs/guides/joins_with_the_builder.md`, the builder section of
   `CLAUDE.md`, and a pointer from `plans/trickle-dag.md`.
5. Trickle's version moves to 0.2.1 (no API change). Releasing it is a separate, approved step.

## As built

Landed 2026-10-18, as planned. Notes:

- Measured through the builder (`bench/trickle_join/`): inner 266 → 72 ms (10k batch) and 538 → 88 ms (250k);
  left 300 → 90 ms and 554 → 107 ms; identical changelogs before and after. The builder sits 25 to 30 ms above
  the hand-written SQL (delta reads, `p` counts, the root consolidation).
- `key_filter=False` no longer changes anything for a join over two sources; it only stops pushdown into a
  subtree. The two tests that pinned the old `IN (SELECT k0 …)` SQL were replaced by pushdown tests (an outer
  subtree with and without `key_filter`).
- First built without pushing through a null-supplying side, on the mistaken view that it was invalid; added
  the same day with tightening, after measuring that case as the most expensive (58 ms against 5). The tests
  check the tightened SQL for `left` and `full` (`test_builder_pushes_keys_through_a_null_supplying_side`); the
  differential test reaches tightening about 270 times over its seeds. Since an untightened filter is also
  correct, the differential test can't tell the two apart: the SQL test is the guard.
- Chains: the rule composes, since each join's delta satisfies `X' = X + δX` exactly (`X'` is the subtree's
  current state, joined from the sources' current states), and the randomised test checks chains and bushy
  shapes with every source changing. The cost didn't compose at first: the join above reads a child's delta
  two or three times, and an inline view is re-evaluated per reference, so a chain of 6 with every source
  changing took 474 ms, slower than recomputing (341 ms). A child join's delta is now materialised into a temp
  table when its parent reads it more than once (`_reads_twice`, dropped in `_chain` after the write): 198 ms.
  The old builder took 26.7 s on the same chain (17.2 s with only the fact changing), so it was exponential
  too. `bench/trickle_join/chain.py`; `test_a_chain_computes_each_intermediate_delta_once`.
- Through the builder, `(fact ⟕ prod) ⋈ cat` on `prod.cat_id` with 5 of 5,000 categories changed (15M facts):
  46 ms with pushdown against 104 ms with `key_filter=False`, the same changelog.
- The differential test (`test_trickle_join_delta.py`, 40 seeds plus a flip-set case) was checked by breaking
  the rule on purpose: the `δL ⋈ δR` sign, the semi/anti flip sign, and the flip-set condition each fail it.
- Trickle is 0.2.1 in `packages/trickle/pyproject.toml` and `uv.lock`; not released.
