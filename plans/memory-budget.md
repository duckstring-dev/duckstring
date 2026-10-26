# Memory budget: sharing one machine's memory between its Ducks

Status: **designed 2026-10-27, after measurement; replaces the 2026-10-24 proposal** (leases sized from each
Pond's history), which the author rejected as fragile. The design here takes no estimate of a Run's memory
from history, data sizes or plans: a Catchment-level concurrency limit with memory slots, gated at the
engine's start decision. Not built.

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

## What DuckDB does with a limit changed under a running query

Measured 2026-10-26 on the laptop (DuckDB 2.0.0, a 120M-row table, each case in its own process, twice;
the script is in the session notes, summarised here). A 10 GB high limit and a 2 GB low one:

| Workload | 10 GB throughout | 2 GB throughout | 10 GB lowered to 2 GB at about 1.7 s | 2 GB raised to 10 GB at about 1.8 s |
|---|---|---|---|---|
| Hash join | 5.1 s | 7.7 s | refused until the join ended | failed, out of memory (2 of 2) |
| Aggregate | 5.0 to 5.3 s | 23.2 s | the `SET` blocked 5 s, until the aggregate was nearly done | 6.4 to 6.7 s |
| Sort | 5.7 to 5.9 s | 11.0 s | refused until the sort ended | 6.3 s |

And the lowest limit DuckDB would accept about 1.8 s into each query (bisection; each `SET` blocks about a
second while DuckDB evicts):

| Workload | Lowest accepted | Query time after (10 GB throughout) |
|---|---|---|
| Hash join | 7.6 GB of 10 | 5.3 s (5.1 to 5.3 s) |
| Aggregate | 0.65 GB | 7.1 s (5.2 to 5.3 s) |
| Sort | 0.65 GB | 51 to 115 s (5.3 to 5.6 s) |

- `memory_limit` bounds the process: resident memory was the limit plus about 1 GB.
- A running query can't be resized dependably. A lower limit is refused, or blocks, until the query frees
  enough; how much it can free and what that costs depends on the operator (a sort accepted the cut and ran
  10 to 20 times slower). Raising the limit mid-join failed with "failed to allocate data of size 8.0 KiB
  (2.4 GiB/9.3 GiB used)", which looks like a DuckDB 2.0.0 bug (not reported yet).
- So a Duck's limit is set only when it has nothing running: at the start of its first Run, and back to an
  idle floor when it has none. A DuckDB query is a *moldable* job in scheduling terms (it adapts to any
  memory it is given at the start, by spilling), not a *malleable* one (resized while it runs).

## Background

- **Thrashing and load control** (Denning): once the working sets of the jobs running together no longer fit,
  throughput collapses, and the remedy is fewer jobs at once. The spill cliff in the table above is the same
  shape: the first build went from 35 s to six minutes below about 4 GB per Duck.
- **Memory for concurrent queries** (Mehta and DeWitt, VLDB 1993): a hash join's time is roughly a step
  function of its memory, and giving some queries what they need and queueing the rest beats giving every
  query a share in between.
- **Concurrency slots** are how warehouses do it: Amazon Redshift's original workload management (a queue's
  memory divided equally between its slots, a query beyond them waits; later a query could take several
  slots, `wlm_query_slot_count`, because a lone big query in one slot spilled with memory idle), Snowflake's
  `MAX_CONCURRENCY_LEVEL` (default 8; lowering it gives each query more), Airflow pools, Spark slots.
- Every DuckDB Run uses all the cores already, so beyond a few Runs at once concurrency adds spill and disk
  contention, not throughput. A few at once still pay: small incremental Runs overlap their fixed costs (a
  Duck's start-up, Python), and Runs bound by S3 reads leave the cores idle.

## Design

### Slots

- A machine that runs Ducks has a budget (physical memory times a fraction, less a reserve for the Catchment
  and the OS; `DUCKSTRING_MEMORY_BUDGET` overrides) and `N` slots (the Catchment's `DUCKSTRING_RUN_SLOTS`,
  default 4), each worth budget/`N`. The slot group is the Pond's `pool` (`"catchment"` for the Catchment's
  own machine); a remote Duck alone on its machine needs none (it already gets `DUCKSTRING_MEMORY_LIMIT` at
  about 80% of its task).
- A slot belongs to a **Duck**, not a Pond Run: `memory_limit` is per process, and a Pond whose Runs pipeline
  runs them in one DuckDB instance. A Duck takes its slots when it goes from idle to busy (its Pond starts a
  Run with nothing in flight) and gives them back when nothing is in flight. A second, pipelined Run of a
  busy Pond takes no new slot. A pass (no Duck), a Draw and a Spout take none.
- A Duck's grant is fixed while it is busy (see the measurements). It applies `SET memory_limit` (and,
  later, `threads`) for its grant before its first Ripple, and an idle floor when it has nothing in flight,
  which releases DuckDB's cache.

### Admission, at the start decision

The gate is in the engine's start decision (`sentinel`), not on a `begin_run` already made. A Pond that is
ready to start (its `can_start_pond`) and would dispatch to a Duck that is idle needs a slot; without one it
**isn't started**, and its demand keeps collecting. Demand is a set, so when the slot comes the Run stamps
its Sources' freshness *then* and clears every demand at or below it: requests that arrived while it waited
collapse into one Run on the newest data. (Holding a Run already started would stamp its freshness when it
started, let later demand start pipelined Runs behind it, and leave no safe way to skip the stale one: telling
the engine a freshness completed with no data published lets a Sink stamp old data as new.)

- **Order**: FIFO by the moment a Pond became ready and couldn't get a slot (`PondState.slot_wait_since`),
  ties by Pond id. A Pond goes to the back each time its Duck goes idle, so an Inlet that finishes and is
  ready again queues behind a Sink already waiting: data always gets through.
- **No backfilling**: when the head can't fit (it needs more slots than are free), nothing behind it starts.
  An exclusive request (below) therefore waits only for the slots in use to drain.
- **Batching**: Ponds that become ready in the same pass (siblings whose Source just finished, Inlets a
  Refresh reached together) are admitted together and share the free slots. A short window opened by the
  first arrival (a few ms, at the Driver) can widen that later; it isn't needed for the cases measured.
- **Several slots for a lone Run**: at the end of a pass, once demand has finished flowing, slots still free
  with nobody waiting go to the Ponds admitted in that pass, in queue order, keeping one free while any other
  Pond in the group has outstanding demand (always so under a continuous Refresh). Shared any earlier, the
  first Ponds admitted take slots that Ponds the same pass makes ready a moment later need: the throttle
  passes demand upstream after the starts (found by the "no gate is unchanged" test, with 100 slots).
- **A busy Duck yields**: while another Pond in its group waits for slots, a Duck with a Run in flight starts
  no further pipelined Run; it drains, goes idle, gives its slots back and queues like anyone else. Without
  this, a Pond whose Runs overlap under a continuous Refresh is never idle and starves the queue (found by a
  test: the other Pond never ran in 60 s).
- **After a reload** the slots (soft state, like the throttle's) are restored for the Runs in flight
  (`restore_slots`), so nothing is admitted over the Runs a restart resumes. So the 1 TB first build's join gets three of
  four slots (about 147 GB of a 196 GB budget, above DuckDB's 110 GB peak), and a late arrival still finds one.

### The throttle counts waiting as running

The throttle marks a Ripple slow when two demands reach its Pond during one of its runs. A Pond waiting for a
slot isn't running, so without a change the throttle wouldn't see it as a bottleneck and upstream would keep
running Runs the waiting Pond will skip. So a Pond waiting for a slot counts its **root Ripples as busy** for
the throttle: demand reaching it while it waits counts against them (`_count_demand`), they don't clear as
idle (`_settle_throttle`), and as a bottleneck it releases upstream once they start a new run, which is when
its slot comes. Outside the throttle, waiting is not running (the Inlet rule, liveness and status unchanged).

### Memory outside DuckDB, and failures

- A Python Ripple that holds data outside DuckDB declares it, `@ripple(memory="6GB")` (a Pond default in
  `pond.toml`); a Pond's need is the slots that covers, rounded up (`Pond.slot_need`, default 1).
- A Ripple that fails for memory (a DuckDB `OutOfMemoryException`, or a Duck the kernel killed, `-9`) is
  retried once by the Catchment, separately from the user's failure budget (which defaults to 0; running out
  of memory isn't a fault in the Ripple's logic), asking for every slot (`PondState.slot_exclusive`). Failing
  again with the machine to itself is a genuine failure, reported as such, pointing at `memory=` for a kill.
- A Duck parked on a missing Source asset rolls its Run back (`block_on_missing_asset`), so it holds no slot;
  a failed or killed Pond holds none. (Otherwise four parked Runs could hold every slot while the Source they
  wait for queued behind them.)

### Status

A Pond waiting for a slot reports "waiting for memory" on `/api/status`, distinct from "queued" (demand
unmet), with its place in the queue.

## Building it

1. **Engine** (built 2026-10-27, `tests/test_engine_slots.py`) (`engine/core.py`, `engine/catchment.py`): `EngineState.slot_capacity` (`{pool: N}`; empty =
   no gate, so the Playground and every existing test are unchanged), `Pond.slot_need`, `PondState.slots`,
   `slot_wait_since`, `slot_exclusive`; admission in `sentinel`; the throttle's busy predicate. Tests: the
   scheduling cases below, in `tests/test_engine_slots.py`.
2. **Driver and Duck**: the budget and `N` from the environment; `slot_capacity` for the Catchment's pool;
   the grant (slots times budget/`N`) on the `begin_run` job; the Duck's `SET memory_limit` at idle-to-busy
   and its idle floor; status; the memory-failure retry.
3. **Declarations**: `@ripple(memory=)`, the `pond.toml` default, `slot_need` at deploy.
4. **Shakedown**: SF1000 with `N` = 1, 2 and 4 (first build and change), on the r7gd.8xlarge and a 128 GB
   machine.

## Scheduling cases to test (engine)

- No gate configured: identical to today (the throttle cross-check and the engine suite).
- Never more slots in use than `N`, at every step, across random graphs, Refreshes and durations.
- A Pond that waited starts at its Sources' freshness when admitted, and one Run clears every demand it
  collected; the Runs it skipped are never run.
- FIFO: a Sink waiting behind a continuously refreshed Inlet gets the next free slot; data reaches the Outlet.
- Siblings released together share the free slots; a lone Run takes the spare slots but leaves one while
  other demand is outstanding.
- An exclusive request waits for the slots to drain, blocks later arrivals meanwhile, then runs alone.
- Passes, Draws and Spouts take no slot; a failed, killed or parked Pond gives its slot back; a pipelined Run
  of a busy Pond takes no new slot.
- The throttle: demand arriving while a Pond waits marks it slow and paces upstream to it.
- Progress: when demand stops, every Pond's demand is eventually met (no deadlock), for `N` from 1 to 4.

## Open

- Report the mid-join raise failure to DuckDB with a minimal reproduction (needs the author's go-ahead).
- DuckDB 1.5: spill file names per process, or a spill directory per Duck.
- Pools: the same slots in the Pool agent for a Pool instance running several Ducks.
- The budget's fraction and reserve (0.8 and 2 GB to start), the idle floor, and `N`'s default from the
  shakedown.
