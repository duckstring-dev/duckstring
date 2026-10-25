# Memory budget: sharing one machine's memory between its Ducks

Status: **proposed 2026-10-24**, from the SF100 resource profile of the TPC-DS benchmark (duckstring-blog
`bench/tpcds_chain/resources.py`). Not started. The direction (a lease per run, DuckDB's share estimated and
elastic, everything outside DuckDB declared by the user, no process sampling) was agreed with the author; the
details below are open.

## Why

Every Duck is its own DuckDB process, and each one sizes itself as if it owned the machine: `memory_limit`
defaults to 80% of RAM and `threads` to every core. Nothing divides the machine between them.

- **Memory overcommits.** Six Ducks can each promise themselves 80% of RAM. DuckDB only manages memory inside
  its own limit, so it never sees the others. On a Mac the excess becomes compression and swap (slow, survives).
  On Linux with no swap, the kernel kills a process, usually the largest Duck, and the Catchment fails or
  retries that Pond's Run. It fails on the unlucky run, not gradually.
- **Warm Ducks hold memory they don't need.** A Duck keeps DuckDB's cache after its Run, and Ducks stay warm
  while a recurring Refresh is active. Capping the Ducks at 4 GB cut `sales_feed`'s resident memory from 5.7 to
  4.7 GB with no change in time.
- **CPU oversubscribes.** The three aggregates ran side by side with 10 threads each on 10 cores. That degrades
  gracefully (the OS shares the cores), but it's wasted switching.
- **The orchestrator doesn't help, by design.** There is no cap on concurrent Pond Runs; demand and the throttle
  pace work by demand, not by resources.

At SF100 on a 32 GB laptop none of this bites. Measured with `resources.py` (peak resident memory of the whole
process tree, DuckDB's spill directories, swap, bytes written):

| Run | Peak memory | Spill | Swap |
|---|---|---|---|
| DuckDB rebuild (one process) | 2.6 GB | none | none |
| Duckstring, mixed change (6 Ducks) | 10.5 GB | none | none |
| Duckstring, append (6 Ducks) | 14.7 GB | none | none |
| Duckstring, mixed, `DUCKSTRING_MEMORY_LIMIT=4GB` | 10.8 GB | none | none, same times, outputs identical |
| Duckstring, append, 4 GB | 14.1 GB | none | none, same times, outputs identical |
| Duckstring, mixed, 2 GB | 9.0 GB | 31.6 GB | first build 368 s (35 s uncapped), change 9.7 s, outputs identical |
| Duckstring, mixed, 1 GB | 6.4 GB | 34.7 GB | first build 400 s, change 11.6 s, outputs identical |

The cliff is steep: between 4 GB and 2 GB per Duck, the first build goes from 35 s to six minutes, nearly all of
it in `sales_enriched` (241 s) and `customer_value` (118 s) spilling. The change barely moves, because its
working set is small. An even split would have put every Duck on the wrong side of the cliff while five of
them sat idle: at that moment only `sales_enriched` needed the memory. That is the case for leases sized per
Run over a static split. (A first 2 GB attempt failed `customer_value` while the machine was also swapping
3 GB under unrelated load; the error was lost with the run's state, and a repeat passed. Worth watching: a
Duck spilling hard on a pressured machine may be failing liveness rather than running out of memory.)

Duckstring's peak is 4 to 6 times DuckDB's, from six resident processes rather than any one heavy step (the
largest single Duck was 5.7 GB). At 1 TB the naive scaling is 100 to 150 GB, which is where overcommit kills
Ducks on a 128 GB Linux box.

## Principles

1. **Never overcommit.** The limits handed out on a machine never add up to more than its budget. This is the
   one rule that turns a wrong estimate from a crash into a slowdown.
2. **DuckDB's memory is elastic.** Joins, aggregates, sorts and windows spill when they reach `memory_limit`, so
   a limit set too low costs time, not correctness. A limit can be changed on a live instance.
3. **Memory outside DuckDB is declared, not guessed.** Python in a Ripple (pandas, a model, Arrow buffers)
   isn't covered by `memory_limit`. Rather than sample processes and infer, the Ripple declares it. Sampling is
   fragile, and a declared number is something a user can reason about and fix.
4. **Estimate from what DuckDB reports.** DuckDB 2.0's profiler reports `system.peak_buffer_memory` and
   `system.peak_temp_dir_size` per query (verified 2026-10-24, via `SET tracked_metrics = ['system.*']`). The
   history comes from DuckDB's own accounting.

## Design

### The budget

One budget per machine that runs Ducks: the Catchment's own machine (the `catchment` Duck target), and later
each Pool instance. It is physical memory times a fraction, less a reserve for the Catchment and the OS,
overridable with `DUCKSTRING_MEMORY_BUDGET`. A remote Duck alone on its own machine (Fargate, EC2 per spawn)
already gets `DUCKSTRING_MEMORY_LIMIT` at about 80% of its task, and needs nothing more.

### Leases, per Pond Run

The unit is the Duck, not the Ripple: a Duck has one DuckDB instance, `memory_limit` is per instance, and its
concurrent Ripples share it. So a lease is granted per Pond Run when the Catchment dispatches its `begin_run`:

    lease = duckdb_estimate(pond, mode) + declared_outside(running Ripples) + base

- `base` is a Duck's fixed cost (an idle Duck with an empty cache), measured once per platform.
- The Duck applies the DuckDB part as `SET memory_limit` on its registry instance at the start of the Run.
- When the Run ends, the lease drops to a small idle floor and the Duck sets its limit to match, which makes
  DuckDB release its cache. That alone reclaims most of what warm Ducks hold.

### Estimating DuckDB's share

At dispatch the Catchment knows the Run's mode before it starts: a bootstrap, Rebuild or coverage miss is a full
computation, otherwise it's incremental. Keep a short history per Pond and mode:

- The Duck reports the Run's peak buffer memory and peak spill (the maximum over its statements'
  `system.peak_buffer_memory` and `system.peak_temp_dir_size`) on `run_completed`, stored on `pond_run`.
- The estimate is a high percentile of that history (p90 of the last N Runs of the same mode).
- A Run that spilled heavily is a sign the estimate is low: the next estimate adds the spill.
- With no history, a Run gets an equal share of the free budget and learns from its first Run.

Later (phase 4), scale by input size: the rows a Run will read are known before it starts (the Sources' change
windows from Parquet footers, the same metadata `window_changed` reads; the Sources' sizes for a full Run).
DuckDB's own cardinality estimates are a last resort: we've seen one put an empty anti-join at 0 rows.

### Admission: the queue

A `begin_run` is dispatched when its lease fits in the free budget. Otherwise it waits, in demand order, and
`/api/status` shows the Pond as queued for memory.

- A Run whose estimate exceeds the whole budget runs alone, with the whole budget, and spills. It never waits
  forever.
- A smaller Run may go ahead of a waiting larger one only if that can't delay the larger one past a bound.
- Admission is separate from the throttle: the throttle decides when demand is sent, admission decides when a
  dispatched Run can start. A Run held for memory counts as started for no rule in the engine; it is a Driver
  concern, between `pending_begin_runs` and the Duck's `jobs`.

### Elastic leases (phase 3)

The broker can change a lease mid-Run: grow a Run that is spilling when memory frees up, shrink a running Run
under pressure (it spills, it doesn't die). The Duck applies each change with `SET memory_limit`. Needs a spike:
how promptly DuckDB applies a lowered limit to a running query, and whether lowering it evicts cached
persistent pages at once.

### Threads

The same lease can set each Duck's `threads`, in proportion to its share, so CPU stops being oversubscribed.
Lower priority: CPU contention degrades gracefully.

### Memory outside DuckDB: declared

A Python Ripple that holds data outside DuckDB declares it:

```python
@ripple(memory="6GB")
def train(pond): ...
```

and a Pond can set a default for its Ripples in `pond.toml`. SQL Ripples, builder Ripples and dbt models never
need it: their work is inside DuckDB. The declared amount is added to the lease while that Ripple runs. It is
not enforced (capping a Python process without cgroups would also cap DuckDB's memory maps); it is a
reservation. Undeclared means zero beyond `base`.

When the declaration is wrong, the failure should say so without sampling anything:

- A Duck killed by the kernel exits with SIGKILL (`returncode == -9`). The liveness check already sees the
  process end; it can report "the Duck was killed, probably for memory" and name the Ripples that were running,
  with a pointer to `memory=`.
- A DuckDB `OutOfMemoryException` (its own limit reached by something that can't spill) names the Ripple
  directly.

## Phasing

1. **No overcommit, cheaply.** A static split of the machine budget between resident Ducks, applied with
   `memory_limit` (the existing `DUCKSTRING_MEMORY_LIMIT` path, set by the launcher instead of by hand); the idle
   floor after each Run; `memory=` on `@ripple`; the SIGKILL message. This alone makes the 1 TB single-machine
   benchmark safe.
2. **History and admission.** Peak buffer memory and spill reported per Run; estimates by mode; the dispatch
   queue.
3. **Elastic leases.** Grow and shrink mid-Run, after the spike.
4. **Input-scaled estimates** from Parquet footers.

## Open questions

- Does tracking `system.peak_buffer_memory` cost anything measurable when on for every statement? It needs
  profiling enabled on the registry instance; the alternative is DuckDB's `duckdb_memory()` read at the end of
  each Ripple (current, not peak, so it would miss the peak).
- DuckDB 1.5: are its spill files also named per process, as 2.0's are (`duckdb_temp_{pid}_…`)? All Ducks on
  a machine spill into `{root}/tmp` when `DUCKSTRING_MEMORY_LIMIT` is set. If 1.5 uses fixed names, each Duck
  needs its own spill directory.
- Pools: a Pool instance running several Ducks needs the broker in the Pool agent. Same design, different host.
- Where the budget's fraction and reserve defaults land (0.8 and 2 GB are guesses).
