# Change-fraction threshold (`p`) on insert-only changes

Over `p` (default 0.3) of a Source's rows in one Run's change, the builder recomputes the whole output and diffs it
against what it wrote. The question (`plans/showcase-fixes.md` item 19's follow-up): should a change that only adds
rows skip that check?

`run.py` bootstraps 4M facts (append) and a 1M-row dimension (merge), adds `frac × N` facts in one Run, and times
that Run with the check on (p=0.3) and off (p=1.0), each from a fresh in-memory DuckDB, best of 2. Both settings'
outputs are asserted equal. Run from the repo root: `uv run python bench/p_threshold/run.py` (`N`, `REPS`, `FRACS`,
`CASES` in the environment).

## Results (2026-11-20, MacBook Pro, 10 cores, 32 GB)

Seconds for the timed Run.

| case | frac | p=0.3 | p=1.0 | ratio |
|---|---|---|---|---|
| join.merge | 0.1 | 0.05 | 0.05 | 0.96 |
| join.merge | 0.3 | 0.06 | 0.06 | 0.96 |
| join.merge | 0.5 | 0.23 | 0.07 | 0.29 |
| join.merge | 1.0 | 0.28 | 0.09 | 0.31 |
| join.merge | 2.0 | 0.38 | 0.13 | 0.34 |
| join.merge | 4.0 | 0.58 | 0.21 | 0.37 |
| join.append | 0.1 | 0.06 | 0.06 | 0.97 |
| join.append | 0.3 | 0.08 | 0.08 | 0.98 |
| join.append | 0.5 | 0.31 | 0.10 | 0.34 |
| join.append | 1.0 | 0.41 | 0.17 | 0.42 |
| join.append | 2.0 | 0.59 | 0.29 | 0.49 |
| join.append | 4.0 | 1.04 | 0.54 | 0.52 |
| aggregate | 0.1 | 0.12 | 0.11 | 0.96 |
| aggregate | 0.3 | 0.18 | 0.18 | 0.97 |
| aggregate | 0.5 | 0.12 | 0.23 | 1.98 |
| aggregate | 1.0 | 0.13 | 0.30 | 2.27 |
| aggregate | 2.0 | 0.16 | 0.42 | 2.60 |
| aggregate | 4.0 | 0.22 | 0.67 | 3.05 |
| join.merge+dim | 0.1 | 0.06 | 0.05 | 0.91 |
| join.merge+dim | 0.3 | 0.08 | 0.07 | 0.94 |
| join.merge+dim | 0.5 | 0.20 | 0.09 | 0.45 |
| join.merge+dim | 1.0 | 0.25 | 0.13 | 0.54 |
| join.merge+dim | 2.0 | 0.33 | 0.22 | 0.67 |
| join.merge+dim | 4.0 | 0.50 | 0.39 | 0.79 |

## Reading

- Joins: past `p`, the incremental path is 2 to 3.5 times faster for an insert-only change, including when the
  dimension changes in the same Run. Joining the new rows is a subset of the recompute's join, and the recompute
  also diffs its whole output.
- Aggregates: past about 0.3, the recompute is 2 to 3 times faster. The incremental aggregate's per-row cost
  (partition moments, per-group state merges) is several times DuckDB's own `GROUP BY`.
- Below `p` the two settings match.

Decision: no change. `p` stays user-set; in streaming shapes a change over 0.3 of a Source should be rare.
