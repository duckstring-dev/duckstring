# Trickle, take two: Z-set / DBSP-style incremental joins

Status: **built.** Implemented in `trickle_io.py` (Z-set changelog, `apply_zset`, `merge_table`,
`read_delta`), `trickle_builder.py` (the DBSP composition), `core.py` (`Pond.merge_table`/`apply_zset`/
`trickle`), and `dataplane.publish_plan` (the per-table `f`-stamped sidecar). Tests: `tests/test_trickle.py`
(incl. the worked fact+dim example, non-PK joins, ripple inputs, the threshold) + the Iceberg cases. The
Trickle/ripple feature was unreleased, so the on-disk changelog/sidecar format changed outright (no
migration). Two notes from the build, folded in below: **(1)** the weight column is `_duckstring_d` (like
every system column), not a bare `_d`; **(2)** the final/comprehensive step reads the **last-written main**
as its prior output (`O_old`) and never recomputes it.

Supersedes the change-detection + key-set propagation core of
`plans/trickle.md` (the `comprehensive` diff, `keys_joining`, the `upsert`/`delete` changelog, the
`Source`/`Join`/`Filter`/`Project` builder with its FK=PK constraint). The orchestration model
(freshness, the `(previous_f, f]` window, retention/floor, the data-plane sidecar) is unchanged — this
only changes *how a Trickle computes its output from its sources*.

This is a behaviour-preserving generalisation: everything the current builder can do, this can do, plus
arbitrary equi-joins on any key, joins onto overwrite Ripples, and multiple sources changing in one run.

## Why

The shipped builder (`trickle_builder.py`) only maintains a join incrementally when it is a star of
**Trickle** dimensions joined to the spine on **`on == dimension PK`** (`trickle_builder.py:70-74`,
`:44-49`). The reason was delete-soundness: a merge changelog stores deletes as **key-only tombstones**,
so a delete can only be propagated by joining on the key it carries. That blocks the most common real
shape — a small, rarely-changing master/reference table joined to a large fact stream on a *business
code* that isn't the master's PK.

The fix is a change of representation, not a new special case: **represent every change as a Z-set
(weighted set) with full-row images**, and compose joins with the DBSP incremental rule. A deletion
becomes a full-row tuple at weight `−1` (not a key tombstone), so it cancels its previously-produced
join/project output *by content*, through any join, on any key. The FK=PK constraint dissolves; one
`.join()` covers everything.

## The representation

A **Z-set** is a relation with an integer weight column `_d` per row. `_d = +1` is a present row,
`_d = −1` a retraction. A normal table is a Z-set with every `_d = +1`. The changelog stops storing
`upsert`/`delete` ops and instead stores Z-set rows directly:

- **append Trickle** — history rows, all `_d = +1`.
- **merge Trickle** — a `_d`-stamped changelog. An **update is stored as two rows**: the old full image
  at `_d = −1` and the new full image at `_d = +1`. (So we no longer need an `op` column, and `merge`'s
  old "upsert + key-tombstone" pair is gone. The `−1` row *must carry the full old image*; that is the
  whole point.)
- The **main** (a merge Trickle's clean current state) is the consolidated positive set — unchanged in
  spirit; it is the integrated history of that source's own output.

`_d` replaces `_duckstring_op`. `_duckstring_f`/`_duckstring_hash` keep their roles (`_d` is a new
reserved system column; the Iceberg invariants in `plans/trickle.md` — UTC `_duckstring_f`, VARCHAR
`_duckstring_hash` — carry over, and `_d` is a small signed int, Iceberg-safe).

### What each source presents per run

For output `O` over sources, each source contributes a **history** `X` (its prior full state) and a
**delta** `x` (its Z-set change over `(previous_f, f]`). Notation below: uppercase = history, lowercase
= delta, `·` = join, `+` = union/append.

| Source kind | `f` unchanged | `f` advanced |
|---|---|---|
| **append/merge Trickle** | `x = ∅`, contributes as stable history `X` (= main) | `x` = changelog window (full-image `±1` rows) |
| **overwrite Ripple** | `x = ∅`, contributes as stable history `X` (= the table) | **no clean `x`** — see below |

A Trickle delta flows through the algebra. A **Ripple has no old state to diff against**, so when it
advances it cannot produce a correct `x` (you'd get only the `+1` side; the retractions of the old
output are unrecoverable from the Ripple alone). It therefore forces the **comprehensive path** for that
run (below). An *unchanged* Ripple is a first-class cheap operand (a stable history) — which is exactly
the master-data common case, so **Ripple inputs are allowed, not banned**. The only way to make a
Ripple's *changes* incremental is to publish it as a native Trickle upstream; until then it is
"stable-history-or-comprehensive", and promoting it later is a zero-touch change to consumers. We do
**not** keep a consumer-side shadow replica to synthesise its delta — that is a hidden per-consumer
materialisation (the same thing we rejected as an inner-join cache), and "one Trickle = one visible
maintained Z-set, nothing hidden" is the rule.

## The delta algebra (DBSP incremental join)

Linear ops are trivial on Z-sets: `filter` applies per row regardless of weight; `project`/`union` add
weights (consolidate later). The join is bilinear; its incremental form for two operands:

```
d(A,B) = A·b + a·B + a·b           (= A·b + a·(B+b)  =  (A+a)·(B+b) − A·B)
```

and it **composes sequentially**, the intermediate result `AB` carrying its own (history, delta) pair:

```
d(A,B,C) = AB·c + d(A,B)·C + d(A,B)·c   (= AB·c + d(A,B)·(C+c))
```

The payoff: when the tail sources are unchanged (`b = c = ∅`), `d(A,B,C) = a·B·C` — one changed source's
small delta joined against stable, indexed histories. That composes to arbitrary depth.

**The load-bearing subtlety — old state is required.** When two operands change in the same run (the
normal case: one run has one freshness `F`, any number of sources can have advanced past `previous_f`),
naïve `a·b` is *wrong* — it is only the cross term and drops `a·B` and `A·b`. The correct evaluation
needs the **prior** state of changed operands. Equivalently, the left-deep telescoping form is:

```
d(F,D) = φ·D_old + F_new·δ
```

`D_old` (the dim's state as of `previous_f`) is **not** the current dim main — using current mains here
silently corrupts (worked below). Reconstruct `D_old` via an as-of-`previous_f` read or `main` minus its
changelog window. This is cheap when the changed operand is a small dimension; it is only ever needed for
operands that *changed*.

### Worked example — fact and dim both change in one run

Prior state (`= main`), output `O = F·D` on `k`, `pk = id`, projection `(id,k,qty,price)`:

```
F:  (1,A,10) (2,A,5) (3,B,7)        D:  A→100  B→200
main(O):  (1,A,10,100) (2,A,5,100) (3,B,7,200)
```

This run: fact updates `id=2 qty 5→8` and inserts `id=4 (B,2)`; dim updates `A price 100→120`.

```
φ:  −1(2,A,5)  +1(2,A,8)  +1(4,B,2)        δ:  −1(A,100)  +1(A,120)
F_new: (1,A,10)(2,A,8)(3,B,7)(4,B,2)       D_old: A→100 B→200
```

Compute `d = φ·D_old + F_new·δ`:

```
φ·D_old :  −1(2,A,5,100)  +1(2,A,8,100)  +1(4,B,2,200)
F_new·δ :  −1(1,A,10,100) +1(1,A,10,120) −1(2,A,8,100) +1(2,A,8,120)
```

Consolidate **by full row** (sum `_d`, drop zeros): the `(2,A,8,100)` pair cancels (`+1` from term 1,
`−1` from term 2 — an intermediate priced at the *old* dim value, which is *meant* to cancel):

```
−1(2,A,5,100)  +1(4,B,2,200)  −1(1,A,10,100)  +1(1,A,10,120)  +1(2,A,8,120)
```

This is exactly the true `ΔO = O_new − main`. The keyed reconcile (next section) yields upserts for
`id 1,2,4`, no deletes. ✔

**The trap, made concrete.** Use the *current* dim (`D_new: A→120`) in the first term instead of
`D_old`:

```
φ·D_new :  −1(2,A,5,120)  +1(2,A,8,120)  +1(4,B,2,200)
```

Now `(2,A,5,100)` is **never retracted** (the `−1` is at `…,120`, a row that never existed) → the stale
row survives in `main`; and `(2,A,8,120)` lands at weight `+2`. Silent corruption — precisely the
`under-merge silently corrupts` failure mode. Hence: old-operand terms use old state, full stop.

## Evaluation order

For a **chain** (`A·B·C`, each linking to the next), a change in a *late* source forces recomputing the
*earlier* intermediate (`d(A,B,C) = AB·c + …` needs `AB` when `c ≠ ∅`). So put **small/stable sources
early and the large table last**, where its prior result is the materialised output (`main`) — free
history. Large table early ⇒ large fact-grained intermediates that a downstream change must rebuild.

For the **star** the builder actually supports (dims join directly to the spine), the spine fact is
structurally in *every* join (two dims can't be pre-joined — no predicate between them), so every
intermediate is fact-grained regardless of order. There the remaining knob is *among the dims*: join the
**most-volatile / least-key-filterable dim innermost** (its change becomes `fact·δ`, key-filtered
directly), stablest dim outermost. Evaluation order is chosen by the builder from stats/hints and is
**decoupled from `.pk()`** (which only declares the grain).

The deep rule both cases share: **every Trickle boundary materialises a history.** "The last join uses
`main` as its history" is the special case; the general way to make any expensive intermediate
cheap-on-downstream-change is to put a Trickle boundary right before it. So *"large last"* and *"make
Trickles often"* are one principle — materialise before something expensive has to be re-derived. A
large reused intermediate is a signal to **split a Trickle**, never to cache.

## Output: consolidation, keyed reconcile, `.pk()`

Consolidate the final delta Z-set **by full row** (`GROUP BY <all output cols> HAVING SUM(_d) <> 0`).
Grouping by key would cancel updates (`−1` old + `+1` new at the same key sum to 0) and lose them — a
real bug in the first sketch of this design. From the consolidated Z-set, derive the keyed changelog:

- **upsert** a key iff it has a surviving positive-weight row (write that row);
- **delete** a key iff it has retractions but **no** surviving positive row (per-key existence, *not*
  `SUM(_d) < 0`).

`.pk()` becomes a **required** builder method (replacing the implicit spine PK). It declares the output
grain and the merge identity. Because `.join()` now admits arbitrary (incl. many-to-many) joins, the
declared key **must be genuinely unique** in the output, or `main` can't hold the rows and
surviving-positive-per-key breaks; validate it in dev/test (`GROUP BY pk HAVING count(*) > 1` on the
recompute).

## Two paths, and what selects them

1. **Delta-compose** (the incremental win): *every* changed source is a Trickle with a clean delta, and
   no source is over its change-fraction threshold `p`. Build the sequential `d(...)` carry, ordered
   volatile-inner, key-filtering applied where FK=PK as a pruning term, reconstructing old state only for
   changed operands.
2. **Comprehensive-against-`main`**: any source is a *changed Ripple*, or it's **bootstrap**
   (`previous_f = NEVER`), **coverage-miss** (`previous_f < floor`), **refresh**, or **over-`p`**.
   Compute the full output `O'` from current source states and reconcile `O' − main` (consolidate by full
   row → upserts/deletes). This is today's `comprehensive=True` and it already exists.

These are the *only* two branches. Crucially, `O' − main` always works because `main` is the one old
state we always have — so "a source can't supply a clean delta" never blocks correctness, it only costs
a full recompute of that Trickle's output.

## Determinism contract

Retractions cancel old output **by full-row identity**. So every projection/join expression must be
**deterministic**: a `now()`, `random()`, unstable `string_agg` ordering, or unstable float/decimal
text rep means a `−1` row won't byte-match the `+1` it must cancel, and stale rows leak into `main`.
This is the standard IVM determinism requirement; state it in the builder contract and reject obvious
non-determinism where detectable.

## API — collapse the builder

- `pond.trickle(ref)` wraps **any** table (Trickle or overwrite Ripple); the
  `must-be-Trickle` / non-PK / snowflake `BuildError`s (`trickle_builder.py:44-49`, `:62-74`,
  `:147-151`) go away (snowflake/multi-hop still needs a downstream Trickle — that's a topology limit,
  not an op-set limit, and stays).
- **`.join()` is the only join method.** It accepts any equi-join on any column(s); delete-soundness now
  comes from full-image retractions, not the key.
- **`.pk(...)` required.** `.filter()`, `.select()` unchanged.
- **Aggregation stays out** (see below) — keep `revenue`-style aggregates as a downstream comprehensive
  Ripple/Trickle, as today.
- `p` (change-fraction threshold) keeps its meaning: over-`p` on any source ⇒ comprehensive path.

## Plan

1. **Changelog → Z-set rows.** Replace the `upsert`/`delete` + `_duckstring_op` format with `_d ∈
   {−1,+1}` full-image rows; update writes (`append_table`/`merge_table` in `trickle_io.py`), the
   `_trickle.json` sidecar version, the registry meta, and the Iceberg append-commit path. Migration:
   bump the sidecar schema; a Trickle published in the old format is read once via comprehensive (its
   `main` is intact), then re-emitted in the new format.
2. **Source → Z-set materialisation.** `to_zset(source, previous_f, f)`: append → `+1` window; merge →
   `±1` changelog window; unchanged → stable history; Ripple-changed → flag for the comprehensive path.
   Column-prune to the used set (sound under consolidate-at-end; never `DISTINCT` before consolidating).
3. **Incremental join operator.** Implement the sequential `d(...)` carry with volatile-inner ordering;
   reconstruct old state of changed operands via as-of read / `main`-minus-window (**never** current
   mains for old-operand terms); FK=PK key-filtering + `p` as an optional pruning term.
4. **Consolidation + keyed reconcile.** Group-by-full-row → surviving-positive ⇒ upsert; retracted key
   with no surviving positive ⇒ delete. Idempotent re-apply at the same `F` (crash replay / immediate
   retries).
5. **Collapse the builder API.** Drop the `BuildError`s above; add required `.pk()` + uniqueness
   validation; deterministic-projection contract; ripple inputs admitted.
6. **Tests, leading with the cases that broke the first sketch:** fact-only delta (`a·B·C`); **fact and
   dim both change** (the worked example — old-state reconstruction + intermediate cancellation);
   changed Ripple ⇒ comprehensive; delete/update propagation through join+project on a **non-PK** key;
   fan-out `.pk()` violation; bootstrap/coverage-miss; replay-at-`F` idempotency; the over-`p` fallback.
   Mirror across Parquet and Iceberg planes.

## How close is this to a "real" DBSP engine?

**Semantically, for its supported query class, it is genuine DBSP** — and that's the part most people get
wrong, so it's worth saying plainly. It has the actual core:

- Z-sets with integer weights and **full-image retractions** (the thing that makes `distinct`/delete
  composition work).
- Linear operators on Z-sets; the **bilinear incremental join** with the correct `A·b + a·B + a·b`
  expansion and old-state operands.
- **Sequential delta composition** through a join DAG (the chain rule / lifted incremental operator).
- **Consolidation** at the output boundary (DBSP's `distinct`/`consolidate`).
- **Materialised integrated state** at Trickle boundaries (DBSP's integrator `I` — every Trickle `main`
  *is* a maintained integral).
- Bootstrap = the "from empty" first step; retention/floor = trace GC.

So for **acyclic equi-join + filter + project + set-output** queries with **batch-at-`F`** semantics, it
computes the same incremental answer a real DBSP circuit would. That's an *okay* engine for that class.

What separates it from Feldera/Differential-Dataflow-class systems — the things genuinely **missing**, in
rough order of how much they'd hurt this product:

1. **Incremental aggregation / non-linear operators.** SUM/COUNT are linear, but MIN/MAX, DISTINCT, AVG,
   percentiles need dedicated incremental operators that handle retraction (e.g. MIN must know the
   runner-up when the current min is retracted → a maintained sorted/indexed structure). This design
   punts all aggregation to a downstream comprehensive step. For analytics — where the *output* is
   usually an aggregate — this is the most impactful gap. **The single highest-value next step toward a
   real engine.**
2. **No persistent indexed traces (arrangements).** A real engine keeps indexed, incrementally-maintained
   operator state (DD's arrangements / Feldera's spines), so a join is an index-probe against maintained
   state, not a re-query. Here the "integral" is a DuckDB table re-read and re-joined each run, and old
   state is *reconstructed* per run. This is **micro-batch IVM expressed in SQL**, not a maintained
   dataflow circuit — correct, but with higher constant factors, and it's the direct cause of the "a dim
   change still has to touch the big fact" cost. Adding indexed traces is the path to closing that.
3. **No recursion / fixed-point.** DBSP's headline capability — incremental recursive queries
   (transitive closure, Datalog) via nested time domains and the fixed-point operator — is absent, and
   the orchestration layer forbids cycles anyway. Rarely needed for transform pipelines, so low priority.
4. **No automatic incrementalisation.** DBSP mechanically transforms *any* query `Q` into `Qᐩ`. Here the
   incremental rules are hand-written for a closed op set (`Source`/`Join`/`Filter`/`Project`). Extending
   the class is manual work, not a compiler pass.
5. **Micro-batch, not streaming.** One logical timestamp per Pond Run (`F`); all source deltas in a run
   are one batch. No sub-batch ordering, late-data handling within a time domain, or streaming-grade
   latency.

The honest framing: **this is the DBSP *incremental-join core*, done correctly, over an acyclic
aggregation-free class, with SQL recomputation standing in for maintained indexed state.** It is
deliberately *not* a streaming dataflow runtime — building one would contradict the product's positioning
(not an orchestration/execution framework; the Catchment is not the product) and its single-node scope.
The two upgrades that would most move it toward "a real engine" without crossing that line are
**(1) incremental aggregation** and **(2) indexed traces for the integrated state** — and notably, the
"make Trickles often / materialise at boundaries" discipline is the cheap, in-philosophy substitute for
(2): a Trickle boundary *is* a persisted, reused arrangement, just coarse-grained and operator-agnostic.

## Open questions

- **`.pk()` uniqueness** under fan-out joins: validate-always (cost) vs dev/test-only (trust in prod)?
- **Old-state reconstruction** mechanism: as-of-`previous_f` read (needs the data plane's as-of seam,
  already wired) vs `main`-minus-changelog-window (registry-only, no plane round-trip). Probably the
  latter for merge sources, the former for others.
- **Migration**: do we rev the sidecar format in place (read-old-once, re-emit-new) or gate behind a flag
  for a release? Leaning read-old-once — the old `main` is intact, so the first run is just a
  comprehensive re-emit.
- Whether to keep `affected_groups`/`keys_joining` as public partial-path helpers at all, or let the
  builder fully subsume them (they become internal to the delta-compose path).
