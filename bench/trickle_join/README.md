# Trickle join delta benchmark

How long the builder takes to compute one incremental run of a join, before and after the per-term join delta
(`plans/trickle-join-delta.md`). The pipeline is the demo's `priced`: an append-only `order_line` table joined
to a `product` catalogue (merge) on `product_id`, at 10 times the demo's size.

| | Rows |
|---|---|
| `order_line` | 15M at bootstrap, then one batch of 10k or 250k new lines |
| `product` | 900k (every tenth product id is missing, so a left join has unmatched rows) |
| repriced products in the run | about 1,000 |

`run.py` bootstraps the output, writes the next batch and repricing, then times the run three times per
variant (median reported), deleting the run's changelog rows between attempts. In memory, DuckDB 1.5.6, Apple
M-series laptop. Every variant prints the changelog it wrote (rows, net weight), and all of them agree.

```
uv run python bench/trickle_join/run.py               # inner join, batches of 10k and 250k
HOW=left uv run python bench/trickle_join/run.py      # left join
```

The "before" numbers come from the builder at commit `e3414ff2`, run by putting that tree's
`packages/trickle/src` first on `PYTHONPATH`.

## Results (2026-10-18)

| Join | Batch | Before | After | `ivm=False` (recompute + diff) | Hand-written per-term SQL |
|---|---|---|---|---|---|
| inner | 10k | 266 ms | 72 ms | 703 ms | 47 ms |
| inner | 250k | 538 ms | 88 ms | 676 ms | 58 ms |
| left | 10k | 300 ms | 90 ms | 794 ms | |
| left | 250k | 554 ms | 107 ms | 808 ms | |

Before, the builder restricted both sides to the union of the changed keys and joined the new and the prior
states. The 250k batch touched about a fifth of the products, so about a fifth of all past orders entered both
joins and cancelled; at 250k it was barely faster than a full recompute. The prior state of `order_line` was
also rebuilt in full (140 ms on its own). After, each term joins a delta to a current state, so the work
follows the size of the change. The gap to the hand-written SQL is the builder's fixed cost: reading the
deltas, the `p` threshold counts and the root consolidation.

`key_filter=False` was 2 to 3 times slower than the default before; now it only matters for a join below an
outer join (next section), and here it makes no difference.

## Chains of joins

`chain.py` joins a 5M-row fact table to N dimensions of 100k rows in a left-deep chain, with 10k new facts and
(unless `ONLY_FACT=1`) 1 in 1,000 rows of every dimension changed. Nothing between the joins is stored, so
each run works from the sources' changes alone.

| Joins | Before, all change | Before, only facts | After, all change | After, only facts | `ivm=False` |
|---|---|---|---|---|---|
| 2 | 278 ms | 231 ms | 57 ms | 31 ms | 149 ms |
| 4 | 1,782 ms | | 118 ms | | 179 ms |
| 6 | 26,742 ms | 17,228 ms | 198 ms | 72 ms | 341 ms |
| 8 | | | 290 ms | | 534 ms |

Before, the cost grew exponentially with depth and was worse than a full recompute from two joins on. After,
it grows linearly, about 45 ms per join when everything changes. Each join's delta is an inline view, and the
join above can read it twice (`δL ⋈ R'` and `δL ⋈ δR`) or more (pushed keys, the existence terms); inline,
that re-evaluates the bottom delta 2^depth times (474 ms at 6 joins, slower than recomputing). So a child
join's delta is materialised once into a temp table when its parent reads it more than once, and left inline
when it reads it once (the only-facts case, where materialising cost about 1.5 ms per join).

## Pushing keys into a subtree

`nested.py` times one delta term, `(O ⋈ P)' ⋈ δC`, with 15M rows in `O` and a change to 1 in 1,000 rows of `C`,
with and without first filtering `O` to `δC`'s keys.

| `P` joined by | No pushdown | Keys pushed into `O` |
|---|---|---|
| inner join | 15 ms | 13 ms |
| left join | 60 ms | 16 ms |

`nested.py` also times a key owned by the NULL-padded side, `(O ⟕ P) ⋈ δCat` on `P.cat_id` with 5 of 5,000
categories changed:

| | Time |
|---|---|
| no pushdown | 58 ms |
| keys pushed into `P`, left join kept | 9 ms |
| keys pushed into `P`, left join tightened to inner | 5 ms |

DuckDB reorders an all-inner subtree so the small side is joined first, but it can't move a join below an
outer join. So the builder pushes keys into a subtree only, never into a single table, where the join already
does the filtering. Through the padded side of an outer join it tightens the join first: the rows the filter
would pad have a NULL key and never match the change, so producing them is wasted work.
