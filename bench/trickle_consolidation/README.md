# Consolidation on the Trickle read path

How much consolidation costs when the builder reads a merge Trickle, and whether a join could skip it. `run.py`
builds a merge Trickle `catalog` of 1M products, bootstrapped and then repriced in runs of about 1,000 products
each, and times (in memory, DuckDB 1.5.6, Apple M4, median of 5):

- reading its current state (latest-per-key over base and changelog), with and without a checkpointed base;
- consolidating a change window, as `read_delta` does;
- the join term `ΔA ⋈ B′` for 10k new order lines, with `B′` produced three ways, each followed by the final
  consolidation of the output: consolidated (as now), base plus the consolidated changelog since the base
  (unioned, not consolidated together), and base plus the raw changelog. All three give the same result.

`WIDE=1` gives the catalog 12 columns, including strings, all carried through the join.

## Results (2026-10-19)

12 columns:

| History | Base | Changelog rows | Read state | `B′` consolidated | base + consolidated | base + raw |
|---|---|---|---|---|---|---|
| bootstrap | no | 1,000,000 | 41 ms | 43 ms | 42 ms | 6 ms |
| bootstrap + 50 runs | no | 1,100,230 | 45 ms | 45 ms | 44 ms | 6 ms |
| bootstrap + 50 runs | yes | 2,126 | 7 ms | 9 ms | 7 ms | 7 ms |
| bootstrap + 250 runs | no | 1,498,908 | 59 ms | 68 ms | 68 ms | 8 ms |
| bootstrap + 250 runs | yes | 2,034 | 9 ms | 11 ms | 8 ms | 8 ms |

Consolidating a change window: 4.0 ms for one run (2,034 rows), 6.5 ms for 50 runs (99,754 rows). With two
columns everything is 3 to 4 times faster (15 ms to read the state with 250 runs of history and no base).

Consolidating changes costs little: change windows are small. What costs is reading the current state of a
merge Trickle that has no base, because that groups its whole history, every read, and grows with it. A base
bounds it: 9 ms against 59 ms here. Duckstring only checkpoints a merge Trickle once it passes 256 MiB
(`DUCKSTRING_COMPACT_THRESHOLD`), so a table below that never gets one, and the standalone host never
checkpoints. Joining the raw changelog avoids consolidation altogether and is fastest, but only an inner join
can take an unconsolidated input: semi, anti and outer joins depend on whether a key is present on net.
