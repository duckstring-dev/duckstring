# Continuous refresh against a Wave

Whether a continuous refresh (a Tide with bound 0, paced by the Pulse throttle) can replace a Wave when run
times vary, before pull is removed from the engine (`plans/refresh-triggers.md`, "Measure first").

## Method

- The 11 topologies of the throttle tests (`THROTTLE_TOPOLOGIES` in `tests/test_engine.py`, dumped to
  `topologies.json`), each driven for 600 s in 100 ms steps by a Wave or a continuous refresh on its Outlet.
- Run-time noise: each Ripple run takes its base duration times `exp(sigma * z)`, `z` standard normal, for
  sigma 0, 0.25, 0.5 and 1 (the playground's variability model). The draw is a hash of (seed, Ripple, run
  number), so both modes see the same duration for the k-th run of each Ripple and the comparison is paired.
- 8 seeds per case. Counts and data ages are over the second half of each run (the steady state).
- Both engines: `py_measure.py` (the Python engine, via the test harness) and `ts_measure.mts` (the
  playground engine, via Node 22.6 or later), with the same noise function. `analyse.py py|ts` summarises.

```bash
python bench/continuous_refresh/py_measure.py 600 8 > bench/continuous_refresh/results/py.jsonl
(cd bench/continuous_refresh && node --experimental-strip-types ts_measure.mts topologies.json 600 8 > results/ts.jsonl)
python bench/continuous_refresh/analyse.py py
```

## Results (2026-10-14)

Ratios are continuous over Wave, means over seeds. Age is the data's age (now minus freshness) at each Outlet
completion.

| Topology | sigma | Outlet runs | Inlet runs | Compute | Mean age, Wave / continuous | p95 age, Wave / continuous |
|---|---|---|---|---|---|---|
| long lead | 0 | 1.00 | 1.00 | 1.01 | 25.0 s / 13.0 s | 25.0 s / 13.0 s |
| long lead | 0.5 | 0.84 | 1.43 | 1.15 | 29.0 s / 14.5 s | 37.2 s / 18.3 s |
| long lead | 1 | 0.74 | 1.37 | 1.02 | 48.2 s / 22.8 s | 73.2 s / 38.2 s |
| deep chain | 0.5 | 0.88 | 1.54 | 1.17 | 35.1 s / 14.6 s | 45.6 s / 20.4 s |
| diamond, long lead on the slow path | 1 | 0.79 | 1.48 | 1.03 | 40.9 s / 20.2 s | 65.0 s / 33.6 s |
| demo | 0.5 | 0.97 | 1.35 | 1.12 | 12.6 s / 11.3 s | 18.1 s / 15.8 s |
| slow sibling Inlet | 0.5 | 1.00 | 1.02 | 1.00 | 9.4 s / 10.0 s | 15.5 s / 16.4 s |
| slow sibling Inlet | 1 | 1.00 | 1.20 | 1.03 | 15.2 s / 17.2 s | 38.1 s / 38.1 s |

Across all 11 topologies:

| sigma | Outlet runs (min, median) | Compute (median, max) | Mean age (median, max) |
|---|---|---|---|
| 0 | 1.00, 1.00 | 1.00, 1.09 | 0.91, 1.00 |
| 0.25 | 0.93, 0.97 | 1.01, 1.24 | 0.90, 1.00 |
| 0.5 | 0.84, 0.97 | 1.04, 1.17 | 0.89, 1.07 |
| 1 | 0.74, 0.95 | 1.03, 1.11 | 0.87, 1.13 |

`python bench/continuous_refresh/analyse.py py` prints the full table.

## Findings

- **Without noise, a continuous refresh matches the Wave's Outlet and bottleneck runs on every topology, with
  data at least as fresh** (two to three times fresher on long pipelines: 13 s against 25 s on the long lead).
- **With noise it completes fewer Outlet runs on long pipelines**: down 16% on the long lead at sigma 0.5 and
  26% at sigma 1, and 13 to 21% on the other long-lead shapes at sigma 1. Short pipelines stay within about
  8% (the demo at sigma 1). A Wave keeps a generation in flight at every stage, which absorbs a slow run; the throttle keeps about
  one, so a slow upstream run can leave the bottleneck waiting.
- **The data is still fresher.** Mean age is no higher in 42 of the 44 cases, and about half the Wave's on long
  pipelines at every noise level (p95 too). Fewer, fresher completions beat more, staler ones for "as fresh
  as possible".
- **The exception is a bottleneck that is itself an Inlet** (slow sibling Inlet): 7% and 13% older at sigma
  0.5 and 1. An Inlet can't hold demand, so pacing falls to the Outlet's completions, which lag the Inlet.
- **It costs more upstream.** Inlet runs rise by up to 62% and total compute by a median of 1 to 4% (at most
  24%): when the bottleneck idles, its slow flag clears and the pipeline refills before detection resumes.
- **The engines agree** on every run count in 638 of 704 runs. The others are the existing pull difference on
  the long-lead diamond under a Wave (32 runs) and Python's minted epoch at busy Inlets (34 runs, within 3
  Outlet runs either way); the ratios above are the same in both.
