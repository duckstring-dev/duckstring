# Launch alignment (prototype, off)

Under a fast Refresh the throttle releases demand at the bottleneck's run starts, so the Inlets start at once and
their data reaches the bottleneck after the lead (Inlet start to the bottleneck's input being ready). When the lead
is shorter than a bottleneck run, the data waits out the rest of it; just over a whole number of runs, nearly a whole
run. That wait is added data age at the same cadence, and it is why a continuous Refresh can be staler on average
than a well-chosen every-N one.

Launch alignment (`launchAlign` in `playground/src/lib/orchestration.ts`, `on: false`) holds a bottleneck's due
release until the slot the data will feed, less the lead and a margin. The lead is read from freshness (when an
input became ready, less its freshness, which is the Inlets' start), so delaying the release doesn't change it: the
estimate has no feedback loop. Estimates are conservative (the longest recent lead, the shortest recent run).
`gate` turns it off when recent leads and runs spread by more than that share of a run.

## Results (2026-10-16)

`node --experimental-strip-types bench/launch_alignment/align_bench.mts` (env `SPREAD`, `GATE`, `WIN`): the throttle
test topologies plus the playground demo, 240 s, second half; a continuous Refresh, one-off Refreshes every
second, and a 1 s Refresh with drifting ticks; σ 0, 0.2, 0.5 (six seeds each with noise); 100 ms ticks and
1000 ms jumps (the browser at 10×). Each row is off → on.

- **Ungated** (`results_ungated.txt`): without noise, ages fall by up to 2.6 s at equal runs (the lead just past a
  whole run gains most); the worst loss is the margin, 0.5 s. With noise it loses runs, up to 13 to 18%, and age.
- **Gated at 10% of a run** (`results_gated.txt`): without noise, ages fall 0.94 s on average and up to 4.2 s; with
  noise about neutral (within 5% of runs, near the spread between seeds). One regression without noise: the
  playground demo under a 1 s Refresh with drifting ticks loses 8% of runs.

The regression is the reason it stays off. Alignment times the Pond's *slow* Ripples, and the slow flag (two
demands in one run) can land on a non-bottleneck Ripple under irregular ticks (`daily_sales` instead of
`join_lines`). The release is then timed to the wrong Ripple, the bottleneck idles, its slow flag clears and the
throttle disengages until it is detected again. Releasing at once, as today, tolerates a wrong guess; a timed
release doesn't. A next step, if it is taken up: time only a Ripple that has stayed slow for several consecutive
runs, and measure again.
