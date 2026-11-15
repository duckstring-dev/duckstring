# Read positions: each Run reads each Source from where the last one stopped

Status: **proposed 2026-11-15, agreed in principle, nothing built.** From showcase-fixes item 1 (a window on an
Inlet hid its Trickle tables from Sinks with other Sources). Code references are to `showcase-fixes` at
`67af6967`.

## The problem

A Sink Run reads its Sources' Trickle tables over one window shared by every Source, `(previous_f, f]`, where `f`
is the Sink Run's own freshness (the minimum over its required Sources) and `previous_f` the previous Run's. Rows
are stamped with the freshness of the Run that wrote them. Two cases go wrong:

- **A windowed Source.** Its Run is stamped with its window's end, in the future, so every row it writes is too.
  A Sink with another, unwindowed Source runs at that Source's freshness (now) and reads nothing of the windowed
  one until the window ends. Outputs that freeze what they saw (an `.append()`, a builder `.append()` join) keep
  the empty join for good: the transit showcase recorded 2,568 observations as unscheduled.
- **Any Source that ran after the Sink's freshness.** `b` last ran at 04:29, `a` runs at 04:30 and so enables
  `c`, which runs at `min(04:30, 04:29) = 04:29` and leaves out `a`'s 04:30 rows until its next Run. Data from the
  Run that enabled a downstream Run should be included.

Plain tables don't have either problem: they are read at the version each Source had published when the Sink Run
was dispatched (today's `source_f` on the job, stored as `pond_run.source_pins`), not at the Sink's freshness.
So one Pond can see a Source's plain table and not its Trickle tables.

An upper bound is needed so that consecutive Runs tile: removing it alone would read the 04:30 rows twice, once in
each of the next two Runs. Stamping rows with the time the Run happened instead of its freshness fixes the first
case only, and `F + p` (freshness as the Run's start plus a pause until the window ends) breaks the minimum rule: a
Sink's freshness would stay at the windowed Source's start for the whole window, so its Runs would share one `F`.

## The model

Each Run reads each Source over that Source's own window: from the **read position** the previous Run reached
(exclusive) to the Source's published freshness when this Run was dispatched (inclusive). The read position is
`changed_f` of the Source at dispatch, the value the job already carries.

- **Current state** (`read_table`, `count_table`, `source_columns`, a builder source's full read): as of the
  Source's read position, for Trickle and plain tables alike.
- **Changes** (`read_delta`, a builder source's delta, `pond.changed()` for a Source): over `(previous read
  position, read position]` for that Source.
- **Own tables** keep the Pond's own window, `(previous_f, f]`: a mid-chain `.merge()`, `read_registry_delta`.
- Row stamps, windows and the engine are unchanged. A windowed Source's rows stamped 06:00 are visible because its
  read position is 06:00; the next Run's window `(06:00, 06:00]` is empty, and the next window's rows arrive once.

Every Ripple in a Run reads each Source at the same position, as now. Positions only climb (`changed_f` does), so
each Source's windows tile and every change is read exactly once. The incremental join and aggregate maths need
each Source's old and new states to be consistent with each other, not a clock shared by all of them; by
induction the previous output is the result over every Source's previous position.

The cost is per-Sink, per-Source state: the read position of the previous Run. Most of it exists (positions are
already carried per Run and stored per Run); what's new is using the previous one as a lower bound.

## Naming

"Pin" is taken in the user docs (a Sink pins a Source's major line and minimum version in `[sources]`). Use **read
position**, the term the Databricks inputs already use for the same idea on an external table
(`core.INPUTS_TABLE`). Rename `pond_run.source_pins` to `read_positions` (a migration), `Pond._pin` to
`_read_position`, and `RunInputs.source_f` and the job's `source_f` to match (a Duck always matches its
Catchment's version). User docs say what happens without the term: "each Run reads every Source up to what it had
published when the Run started, and the next Run continues from there".

## Where the previous positions come from

The Duck already moves `previous_f` up per Ripple (`DuckCore.previous_f_for`): Pond Runs pipeline, so a Run
dispatched while another is in flight gets the same `end_f` from the Catchment, and before 2026-10-23 both read
the same changes. Positions follow the same rule:

- **The job** carries the previous positions: those of the line's newest completed Run (from `pond_run`). A pass
  (`_record_pass`) reads nothing and its Sources are unchanged by definition, so it carries the positions of the
  Run before it.
- **Per Ripple**, the Duck uses its own last completed Run's positions when they are later (a pipelined Run whose
  predecessor completed in this Duck). `RunInputs` keeps positions per Run; the ledger (`pond.db`) records them
  with each completed Ripple so a restarted Duck still has them.
- **A Ripple with no completed Run** (one added by a redeploy) gets no previous positions: it reads its Sources in
  full. That also closes showcase-fixes item 11a (a new Ripple's first delta started at the Pond's last Run). The
  Catchment knows which Ripples have completed (`ripple_run`), so the job can say, and a fresh Duck with an empty
  ledger doesn't take every Ripple for new.
- **Rebuild and Repair**: no previous positions, as `previous_f = NEVER` now.
- **A Force** replaces the Run's positions (as `RunInputs` already does: first job wins unless Force).
- **No job** (`pond run`, Puddles): no positions; reads as of the Run's own freshness, as today.

## Coverage, retention and the rest

- **Coverage** of a delta compares the Source's previous position with the Source's floor, per Source.
- **Retention** (`Driver._retain_from`) already keeps what the in-flight Runs' positions select; lower bounds
  only matter for coverage, which the floor governs. Check the merge read snapshot path (`merge_snapshot` as of a
  position) still finds its base token.
- **The Flock** (`Pond.flock_source_select`) already reads a Source by `source_f`.
- **`pond.previous_f`** stays the Pond's previous freshness for Ripple code. The docs describe Source windows in
  terms of what the previous Run read.
- **Draws** read a Draw like any Source (its `changed_f`).

## Tests

- The showcase case: Inlets `a` (merge table, 6 h window) and `b` (none), `c` joining both; `c` sees `a`'s rows on
  its first Run and exactly once across windows, end to end through real Ducks.
- The 04:29/04:30 case: `c` includes the rows of the Run that enabled it.
- A randomised harness (sources changing and Sinks dispatched in random orders, pipelined Runs, passes, a Force,
  a Rebuild) comparing builder joins, aggregates and `.append()` outputs with a recompute over the same positions.
- A Duck restart mid-pipeline keeps positions; a fresh Duck with an empty ledger doesn't full-read.
- A Ripple added by a redeploy reads its Sources in full once.
- Docs: `reference/python/pond.md` (read_table, read_delta), the incremental guides' description of windows.

Size: L. No engine or Playground change.
