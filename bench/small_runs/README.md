# Many small Runs

Does a Run's time grow with the number of Runs before it? The transit showcase (a Refresh every 31 seconds,
tables of tens to a few thousand rows a Run) saw its Runs slow by about half a millisecond each Run, so within
weeks a Run would have outlasted its cycle.

`run.py` runs two Ponds through the data plane as a Duck does (a fresh cursor of a persistent registry per Run,
then the export):

- `obs`, an Inlet: appends 200 observations a Run, and rewrites a 50-row `stop` dimension that changes once every
  500 Runs.
- `scored`: a builder `.append()` joining each Run's new observations to `stop` (the spine-key fast path), and an
  incremental per-stop aggregate.

```
uv run python bench/small_runs/run.py [RUNS]     # default 2000
```

It prints, per block of 100 Runs, the median Run time of each Pond, the Parquet files each has published, and the
views stored in `scored`'s registry.

## Results

Measured 2026-11-15 on an M-series MacBook (DuckDB 2.0.0, local disk), median ms per block of 100 Runs:

| Runs | `obs` before | `scored` before | `obs` after | `scored` after | files (`obs`, after) |
|---|---|---|---|---|---|
| 100 | 39 | 90 | 33 | 54 | 102 |
| 500 | 73 | 344 | 37 | 75 | 502 |
| 1000 | 87 | 625 | 41 | 107 | 1002 |
| 1500 | 105 | 917 | 43 | 124 | 1503 |
| 2000 | 121 | 1180 | 48 | 148 | 2004 |

Before (0.8.0): `scored` grew 0.57 ms with each Run, the slope the showcase saw, and its registry held 16,007
views by Run 2000 (eight stored per incremental write, never dropped). `obs` also published an empty marker part
per Run for `stop`. The causes, in order of size: a delta read opened every part its Source had published and
filtered to the window; resolving the builder's join keys bound them all too; the change-fraction check counted
the whole Source; the append fast path read the output's history with a row `NOT IN`; and each publish sized every
file. After: 0.05 ms a Run, and 2 stored views (the merge tables' current-state views).

Not flat yet. Each append table still gains a part per Run, and the export lists and reconciles its directory every
Run (`_export_parts`), as do the read snapshot's part listings and the sidecar's size hint. Folding old append parts
into range parts (plans/showcase-fixes.md item 4) is what would make it flat; at 2,800 Runs a day the remaining
slope adds about 0.14 s a day to a Run.
