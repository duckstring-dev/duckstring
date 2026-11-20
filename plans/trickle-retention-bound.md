# A Sink stamped behind its Source can lose rows to the Source's cleanup

Status: **found 2026-11-20, reproduced; closed into read positions** (`plans/source-read-positions.md`, showcase-fixes
item 1). No separate fix: the bug stays live until read positions ship.

## The problem in one example

Three Ponds: `orders` (a fast merge Trickle, refreshed every few minutes), `calendar` (a slow dimension, refreshed
daily) and `report`, which joins them.

1. `calendar` last ran at 00:00. `orders` has run every few minutes since; it is now 15:00.
2. `report` runs. A Run is stamped with the **oldest** freshness among its required Sources (the engine's
   `pond_source_f`, `engine/catchment.py`), so this Run is stamped 00:00, even though `orders` has data to 15:00.
3. `report` reads `orders` **as of 00:00** (`Pond._source_relation`, `read_select(..., as_of=self.f)`). It has to:
   its next Run reads the window (00:00, next], and the windows must tile, or rows are read twice or never.
4. Meanwhile `orders` folds or compacts (its change log passed the 256 MiB threshold). After each publish,
   `gc_tiers` deletes the files no reader can still need, judged by `retain_from`.
5. `retain_from` (`Driver._retain_from`) is the oldest of: `orders`' own last change (15:00), and the **pins** of
   Sink Runs in flight. A pin is the Source's `changed_f` when the Sink Run was dispatched, also about 15:00. So
   the bound is about 15:00, and `gc_tiers` deletes the base and bands that a read as of 00:00 needs.
6. `report`'s read as of 00:00 finds no base at or below 00:00, takes the next newest with a logged warning
   ("no published base at or below …"), and applies `_duckstring_f <= 00:00` to its rows. Every key changed after
   00:00 is missing from the read. No error.

Reproduced 2026-11-20 at the data plane with the `test_read_snapshots` helpers (`DUCKSTRING_COMPACT_THRESHOLD=1`,
22 merge Runs, a reader as of Run 3 while the bound sits at Run 6): the read as of Run 3 is wrong. It is also wrong
with the bound at the reader's own freshness from dispatch on, because the state was already collected before the
Sink Run was dispatched: fixing only Runs in flight isn't enough.

## When it bites

Only when the Source folds (its hot change log passes 2 x `DUCKSTRING_COMPACT_THRESHOLD`) or compacts (warm passes
cold and the threshold), and only for a Sink whose Runs are stamped behind that Source: a Sink with another, slower
required Source. A fast fact table joined to a slow dimension is exactly that shape, so a large enough pipeline
will meet it. Append tables lose nothing today (their parts are only ever removed below the floor). The tiered
unions built 2026-11-20 don't depend on the bound for correctness (a union is read with row predicates; its
constituents wait for the bound to pass the publish that wrote the union), so this is the merge tiers only.

## Resolution (2026-11-20)

The bug is a Sink reading a Source **as of its own, older stamp**. Read positions remove that read: a Sink Run reads
each Source at what the Source had published when the Run was dispatched (its `changed_f` then, the read position),
never at the Sink's stamp. That point is always kept by today's bound:

- before the Sink Run is dispatched, the Source's own `changed_f` is part of `retain_from` at each of its publishes,
  and a later dispatch can only take that or something newer;
- once it is in flight, its read position is part of `retain_from` (`Driver._retain_from`, unchanged).

Between Runs a Sink needs only its window of changes, `(previous read position, read position]`; if a fold has
taken them, the Source's floor turns the read into a full one (correct, slower). So nothing new is retained and no
Source learns anything about its Sinks.

Rejected:
- **A second, lower bound from each Sink's next stamp** (the first proposal here): computing that stamp reads the
  Sink's other Sources, so the Source's cleanup would depend on the graph. A Source may not be aware of its Sinks.
- **A retain file a Sink leaves in the Source's directory** (the author's idea, thought through): it respects that
  rule, but a removed or broken Sink's file freezes the Source's cleanup unless it expires (a lease, with a choice
  at expiry), a Sink would need write access to every Source's storage, the file must stand between Runs (a Sink's
  first Run has none), and it repeats what `retain_from` already carries for Runs in flight.

The reproduction becomes a regression test in read positions: a Sink with a slower required Source, the fast merge
Source folding and compacting (`DUCKSTRING_COMPACT_THRESHOLD=1`) between the Sink Run's dispatch and its read; the
Sink's output equals a recompute.
