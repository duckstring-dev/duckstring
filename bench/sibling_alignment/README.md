# Sibling alignment, the request clock, and multi-Outlet cadence

Refinements to the Pulse throttle (`plans/throttle.md`, "Refinements"), measured on 2026-10-14 in
the Python engine with the run-time noise model of `bench/continuous_refresh/`. The scripts compared each
refinement against the throttle without it through switches in the engine that were removed when the
refinements were adopted; they and their raw results are at commit `52abf511` (`bench/sibling_alignment/`).

## What was measured

- **Noise benchmark** (`align_bench.py`): the 11 throttle topologies plus 7 built for joins (equal and near-equal
  noisy siblings, three siblings, a fast branch that is a chain, a slow sibling behind a lead, nested joins,
  equal branches sharing an upstream), sigma 0 to 1, 8 seeds, 600 s, under a Wave, the old throttle and the
  refined one. Outlet runs, compute, mean and p95 data age, the gap between a join's Source stamps, how often
  the join's throttled Source flips, and the spread of Outlet completion intervals.
- **Liveness fuzz** (`fuzz.py`, `fuzz2.py`): 1,600 random graphs (3 to 10 Ponds, optional Sources, continuous
  refreshes, Tides and Pulses, noise, random Kill, Clear, Sleep, Wake, Force and failures), then every halt
  cleared and the triggers stopped: all demand must drain.
- **Cadence** (`cadence.py`, `cadence_fuzz.py`): several Outlets with distinct cadences over shared upstream,
  each Outlet's mean completion interval against its target and against the same Outlet alone; designed
  scenarios and 200 random graphs (488 Tides).

## Results

- **Sibling alignment** lines a join's Sources up (the gap between their stamps falls two to five times) and
  makes joins fresher: slow sibling Inlet 17.2 s to 16.5 s at sigma 1, a fast branch that is a chain 20.2 s to
  17.5 s, near twins without noise 7.0 s to 5.5 s (the Wave's), nested joins without noise 12.0 s to 9.0 s.
  Topologies without a join are unchanged and compute is the same or lower everywhere. The join's leading
  Source flips no more often than before (equal twins 0.54 to 0.46 at sigma 0.5). Costs: 2 to 5% fewer Outlet
  runs in a few noisy cases (mostly with fresher data) and one regression, a slow sibling behind a lead 6%
  staler at sigma 1. The fuzz found one bug in the first version (a target held back from a Source that then
  became throttled was lost); fixed by tracking held-back targets per Source.
- **A request clock** (counting a Tide from its last request only) put every designed multi-Outlet scenario
  within about 1% of its target, where the existing clock (the later of the pending request and the freshness)
  ran a 10 s Tide beside a continuous neighbour at 12 s and a 20 s one at 24 s. It was not adopted: the author
  prefers that any refresh restarts the count (a strict cadence belongs to an anchored refresh), and the stretch
  is at most one upstream run per cycle. In the random graphs, Tides whose target is at least 1.5 times their
  path's limit run at their target (median 1.000).
- **Known limit**: about 5% of Tides in the random multi-Outlet graphs run slower than target when another
  trigger saturates shared upstream or the bottleneck is several stages away (p99 1.5 times, worst 2.3 times
  at Tides of 2 to 30 s). A throttled trigger Pond releases on its completions, so the stages between it and
  the bottleneck carry one of its requests at a time. The delay is bounded by that lead time, so it matters
  only when the Tide's period is comparable to it. A release turn for trigger Ponds on their Sources' starts was
  tried and rejected: it over-released where the trigger's own demand saturates.
- **Sleep** dropped demand that other Ponds still held (three fuzz cases, with or without the refinements);
  the fix re-sends it to the slept Sources.
