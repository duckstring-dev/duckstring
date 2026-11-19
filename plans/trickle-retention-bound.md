# A Sink stamped behind its Source can lose rows to the Source's cleanup

Status: **found 2026-11-20, reproduced, parked** for the author to decide. Not built.

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

## The proposed fix (not agreed)

Two bounds where there is one:

- **Versions** (`prune_versions`): plain tables are read at the pin, so `retain_from` is right for them and stays.
  Lowering it would keep a full copy of a plain table per Run back to the slowest Source: a day of 1 GB copies in
  the example.
- **Trickle tiers** (`gc_tiers`, the unions' bound): a new, lower bound, the earliest freshness any Sink can still
  read this Source as of: for each Sink, the stamp the engine would give its next Run (the oldest of its required
  Sources' visible freshness), and for each Run in flight, the earlier of its pin and its own freshness. Carried on
  the `begin_run` and `shutdown` jobs beside `retain_from`, and used by the Draw prune.

**Cost.** A Source keeps superseded merge files (old bases, bands, folded change log parts) back to its slowest
Sink's other Source: up to a day in the example. Disk only; nothing more is read.

**Open choice: a required Source that has never run.** Its Sink's next stamp is unknown.
1. Follow the graph: an Inlet that has never run will be stamped at least now; a Pond, the oldest of its own
   Sources. An exact lower bound, and collection never freezes on a Pond that is about to run. (Claude's lean.)
2. Keep everything until it runs. Simpler, but a Source broken from its first Run stops its sibling's collection
   indefinitely.

## Questions to settle first

- Is reading a Source as of the Sink's own (older) stamp the semantics we want, or should a Run's stamp move with
  its fastest Source and the windows be per Source? The second would remove the problem at the root, but it changes
  what freshness means, so it is an orchestration change (see item 1's caution).
- Is a day of superseded files acceptable for the fact-plus-daily-dimension shape, or does it need a cap (and what
  happens to the slow Sink past the cap: a full read, as a consumer behind the floor gets)?

Tests to write either way: the data-plane read above as a regression; a Driver test of the bound in the
fact-plus-daily-dimension shape; an end-to-end Run with a fold forced between the Sink's dispatch and its read.
