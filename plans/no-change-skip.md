# No-change skip: content freshness (`changedF`) and the Pond pass

Status: **built (D1–D4); Trickle empty-delta auto-report deferred.** The engine pass + `changed_f`
(D1, `engine/`), persistence/restore + pass recording (D2, migration `013`, `driver.py`), the Duck
`changed` report + `pond.skip()`/`pond.sources_changed()` (D3, `duck/`, `core.py`), and the
`@ripple(always_run=True)` plumbing (D4, `deploy.py`, reload OR) are implemented and tested
(`tests/test_engine.py`, `tests/test_restart.py`, `tests/test_no_change.py`). **Still to build:** the
Trickle empty-delta auto-report (so a Trickle ripple with an empty composed delta reports
`changed=False` without an explicit `pond.skip()`) — see "Deferred / open". Until then, the quiet
interior is reached by calling `pond.skip()` at the **inlets** (the source of change-truth); every
downstream Pond then engine-passes for free.

Original design follows. This plan adds a second freshness stamp, `changedF`, so a Pond
can signal "I ran but my output did not change," letting downstream skip work — at the engine
level (no Duck spawned) where the no-change can be *proven* from metadata, and at the Duck level
where only the run itself can tell. It is the content-aware *early cutoff* that theory.md's
"Change Gating" motivation gestures at but the current engine doesn't do: today every run advances
freshness, so a Sink always re-runs assuming its Source changed, even when nothing did.

The orchestration principle: **the demand heartbeat and the content signal are different
quantities and must not be conflated.** `startF`/`endF` stay exactly as they are — they advance on
*every* run (pass included), because they are what keeps a Wave re-arming its Inlets and what makes
a Pond Run a real, completing boundary rather than something that appears to hang. We add `changedF`
purely as the *content* mark. A run that genuinely changes output sets `changedF = startF`; a **pass**
advances `startF`/`endF` but holds `changedF`. A chain of passes then propagates downstream for free —
each consumer sees its Source's `changedF` held and passes in turn, advancing freshness without compute.

This is deliberately *not* a new entry in `start a Pond Run when`. Gating the run out (refusing to
start) would stop the Pond re-arming its parents on start, so a Wave path would stall — the Inlets
would never be solicited again. The run still **starts** (advances `startF`, re-arms, stamps Ripples);
what changes is whether it does real work or **passes**.

---

## The state addition

`engine/core.py PondState` gains one field:

```python
changed_f: datetime = NEVER  # freshness at which this Pond's OUTPUT last actually changed
```

Invariant `changed_f <= end_f` always. `NEVER` until the first real run (which always changes —
bootstrap). Persisted in `pond_state` (migration `013_changed_f.sql`, a single nullable column
defaulting to the `NEVER` sentinel text); restored by `Driver.reload`; surfaced in `/api/status`.

Ripples do **not** get a `changed_f` — the content mark is a Pond-output property (the cross-Pond
interchange unit). Intra-Pond, the existing per-Ripple `end_f` machinery is unchanged.

---

## The skip rule (and the exact operand)

A Pond **with Sources** does **real work** on a run iff some Source's content changed since the
freshness this Pond last ran at; otherwise it **passes**. The operand is the crux:

```
priorF = startF            # captured BEFORE startPondRun resets startF = sourceF
sourcesChanged = max(Source.changedF over all Sources read) > priorF
```

- Compare against `priorF` — the Pond's freshness *before* this run — with a **strict `>`**, **not**
  against the freshly-assigned `startF`. A Source that changed exactly at `priorF` was already
  incorporated by the run at `priorF`, so strict `>` is correct.
- `priorF` is the last *run* freshness (pass or real), i.e. the Pond's `start_f` immediately before
  the reset — **not** the Pond's own `changed_f`. A pass legitimately advances "verified up to," so
  the baseline must be the run freshness.

### Why not `>= startF` (the tempting form)

Evaluating `max(Source.changedF) >= newStartF` (against the reset `startF`) is *almost* right and
**unsafe** — it can miss an update from a fresher, non-binding Source. Counterexample. Sink `X`,
required Sources `A`, `B`:

- `A`: `endF 8`, `changedF 6`
- `B`: `endF 10`, `changedF 7`
- `X` last ran at `priorF = 5`

`sourceF = min(8, 10) = 8`, so the new `startF = 8`. `X` reads `B`'s latest output (B@10, content
changed at 7) — a change it has **not** incorporated (last run at freshness 5), so it must do real work.

- `>= startF`: `max(6, 7) = 7 >= 8`? **No → pass.** Misses B's change. **Wrong.**
- `> priorF`: `7 > 5`? **Yes → real work.** Correct.

The two diverge only when a non-binding (fresher) Source's content change lands between `priorF` and
the new `startF` — the diamond / multi-source case. There `>= newStartF` risks a **missed update**;
`> priorF` risks at most a **redundant run** (the rare case where a non-binding Source was already
folded in early). Always pick the operand that can only over-run, never under-run.

**Soundness of `> priorF`** (no missed updates), checked against the freshness algebra: `priorF =
min(Source.endF)` at the last run, so every Source had `endF >= priorF` then, so every change with
`changedF <= priorF` was already visible to that run. The only imprecision is the non-binding-fresh
case, which costs one redundant run, never correctness. In a pure chain (single Source) `sourceF` is
exactly that Source's `endF`, so `> priorF` is exact.

### Sources considered

Use **all Sources the Pond reads (required ∪ optional)** in the `max`, not just required. The
run-trigger gate is unchanged (still required-driven — an optional Source never *triggers* a run), but
including optional Sources in the skip `max` preserves today's behaviour where a triggered run folds in
whatever optional input changed. Soundness is unaffected either way: optional Sources only influence
*pass vs real work* on a run that some required Source already triggered.

---

## Where it lands in the pseudocode

Extends `on starting a Pond Run` (`engine/catchment.py start_pond_run`). The existing body — set
`startF = sourceF`, clear `hasPull`, drop satisfied `targets`, carry `D`, stamp every Ripple — is
untouched. We add the pass/dispatch decision:

```
on starting a Pond Run:
    priorF = startF                      # the freshness this run builds on
    ... unchanged existing body; startF = sourceF; ... ...

    mustRun = isInlet or force_pending or refresh_pending or repairing or always_run
    if mustRun or (has Sources and max(Source.changedF) > priorF):
        dispatch to Duck                 # real work; the Duck reports whether output changed
    else:
        synthesize a pass                # no Duck — complete instantly; changedF held

on completing a Pond Run:
    endF = startF
    if the run changed output:           # a real run that produced new content
        changedF = startF
    # else: changedF unchanged (a pass, or a dispatched run whose output didn't change)
    Pond.endF = min(Ripple.endF)         # unchanged
```

Two execution levels fall out of this one decision:

1. **Engine-synthesised pass** — when the engine can *prove* no Source changed (`max(changedF) <=
   priorF`) and no carve-out forces a run, it completes the Pond Run with **no Duck**: advance
   `startF`/`endF`, hold `changedF`, drop satisfied `targets`, re-arm parents (the heartbeat).
   `Driver` records a `pond_run` row flagged no-change (instant span) so history stays honest — the
   run *did* complete, it just did nothing. This is the big win: the interior of a Waved graph goes
   quiet at the engine level, only the Inlets keep spawning Ducks to poll.
2. **Duck-reported no-change** — when sources *did* change the engine dispatches, but the output may
   still not change (a Trickle whose composed delta is empty, or any Ripple that calls an explicit
   `pond.skip()`). The Duck reports `changed: bool` on the `run_completed` event; the
   Catchment sets `changed_f = start_f` only when `changed` is true. So a dispatched run can still be
   a content-pass, and downstream still benefits.

---

## Carve-outs (always dispatch / always real work)

- **Inlets** (no Sources). The `max` is over an empty set → would read as "always pass," which is
  wrong: an Inlet's whole job is to check external data the engine can't see. Inlets always dispatch
  and determine `changedF` by **content**: a Trickle inlet gets it free (empty changelog ⇒ no change ⇒
  hold `changedF`); an overwrite inlet that knows it didn't change calls `pond.skip()`. Under a Wave,
  Inlets are therefore the only Ducks that keep spawning — and a **window** on the Inlet throttles even
  that (the existing "Wave with Window" result in theory.md, now with a quiet interior).
- **force / refresh / repair.** `force_pending`, `refresh_pending`, `repairing` bypass the skip — their
  entire purpose is recompute-despite-no-change. (Force already resets `end_f`/Ripple `end_f`; nothing
  new needed beyond skipping the pass branch.)
- **`always_run`.** A Pond declaring a side effect that must fire every run (e.g. a monitoring ping)
  bypasses the *engine* pass so the Duck always runs — but the run can still be a *content*-pass via the
  in-Ripple API below.

---

## The API surface

- **`@ripple(always_run=True)`** (default `False`) in `core.py`. Per-Ripple; ORed up to the Pond — if
  **any** Ripple in a Pond is `always_run`, the Pond is never engine-passed (the Duck always runs).
  Declared on the Ripple, stored on `pond_version` like the retry defaults, surfaced to the engine as a
  Pond-level flag.
- **`pond.sources_changed() -> bool`** — true iff some Source changed since this Pond last ran (the
  same `max(Source.changedF) > priorF` test, exposed to Ripple code). For `always_run` Ponds to gate
  their data work:

  ```python
  ...side effects that run every time...
  if not pond.sources_changed():
      pond.skip()        # mark this run a content-pass; downstream skips
      return
  ...normal compute...
  ```

- **`pond.skip()`** — marks the current run as producing no change; the Duck reports `changed: False`,
  the Catchment holds `changed_f`. (Named `skip`, not `pass` — `pass` is a Python keyword.) For a plain
  Ripple with no `always_run`, `skip()` is rarely needed (the engine already passes it when no Source
  changed); it exists for the side-effect pattern and for an overwrite Inlet that has determined no
  external change itself.

Trickle ponds need **no** API: an empty composed delta already means no change, so the Duck reports
`changed: False` automatically. This is the "skip from metadata" the design set out to get.

---

## Push / Tide

The skip rule applies uniformly to push runs. `min(targets)`-clearing already keys on the advancing
`startF`, so a **pass satisfies a Pulse/Tide**: the consumer asked for "data no older than `T`,"
freshness reaches `T`, and the content simply happens to be identical. A Pulse through an
all-unchanged pipeline therefore has every interior Pond engine-pass (freshness climbs to `T`, no
Ducks) and only the Inlets poll to confirm. Nothing in the push propagation (`pond_add_target`, eager
upstream forwarding) changes.

---

## Persistence, transport, history

- **Migration `013_changed_f.sql`** — `pond_state.changed_f` (UTC ISO-8601 text, sentinel `NEVER`).
- **`Driver.reload`** restores `changed_f` from `pond_state` alongside the other freshness fields.
  Migration `013` **backfills `changed_f = end_f`** for existing rows — treat each Pond's last
  completed run as a change, so an upgraded deployment doesn't do one redundant real run per Pond
  before settling. (Defaulting to `NEVER` would be sound but pessimistic; `= end_f` is the honest
  seed since the last run did, as far as we know, produce its output.)
- **Duck protocol** (`duck/core.py`, `routes/duck.py`): the `run_completed` event carries `changed:
  bool`. A Trickle ripple sets it from its delta emptiness; an overwrite ripple defaults `True` unless
  the Ripple called `pond.skip()`. `Driver` applies it in the same handler that advances `end_f`.
- **Run history**: a pass (engine-synthesised or Duck-reported) is a **successful** `pond_run` with a
  `changed = 0` flag and an instant span — so it clears failure episodes normally and is visibly a
  no-op in the UI rather than a phantom. `ripple_run` rows are absent for an engine-synthesised pass
  (no Duck), present-but-trivial for a Duck-reported one.
- **`/api/status`** adds `changed_f` per Pond (and the derived `sources_changed`), so the UI can badge
  "passing / no change" distinctly from "idle" and "running." Edges could render a passing Source
  differently (held content vs advancing freshness) — a UI nicety, not required for correctness.

---

## Worked examples

- **Wave on a chain `A → B → C`, nothing changing.** `A` (Inlet) is solicited each cycle, polls,
  finds no change → holds `changedF`, passes (Duck runs but reports `changed: False`). `B`:
  `max(A.changedF) > B.priorF`? No → **engine-pass, no Duck**. `C`: same. Steady state = only `A`
  spawns a Duck; the interior is synthesised passes that still advance `startF`/`endF` so the Wave
  keeps re-arming `A`. The spin concern collapses to "the Inlet polls; nothing else wakes." A window
  on `A` throttles even that.
- **Diamond, `B` genuinely changes** (the operand example above). `> priorF` fires real work at `X`,
  which reads B@7, output changes, `changed_f_X = startF = 8`. `X`'s downstream then sees `changedF 8 >
  its priorF` → real work; the change propagates. The `>= startF` rule would have stalled it at `X`.
- **Tide on an unchanged pipeline.** Targets propagate eagerly to the Inlets; Inlets poll and pass;
  every interior Pond engine-passes its freshness up to the target; the Tide is satisfied with no
  interior compute.

---

## Deliverables (ship order)

1. **`changedF` + the engine pass** — `PondState.changed_f`, the `start_pond_run` pass/dispatch
   decision, completion stamping, the engine-synthesised pass path. Gate: `tests/test_engine.py`
   (steady-state chain quiet-interior; the diamond operand case; push-pass satisfies a target;
   carve-outs force a run). Pure-engine, no Duck/DB — lands first and is the correctness core.
2. **Persistence + restore** — migration `013`, `Driver` apply/restore, `/api/status` enrichment.
   `tests/test_restart.py` (a passed Pond restores `changed_f` and doesn't redundantly re-run).
3. **Duck-reported no-change** — the `run_completed` `changed` flag, Trickle empty-delta auto-report,
   `pond.skip()`. `tests/test_duck.py` + a `tests/test_runtime.py` e2e (a Trickle whose Source
   republishes with no content change passes downstream without spawning interior Ducks).
4. **`always_run` + `sources_changed()`** — decorator plumbing through `pond_version`, the Pond-level
   OR, the in-Ripple accessors. `tests/test_runtime.py` (a side-effect Ripple fires every run while its
   data work passes).
5. **Docs** — theory.md "Change Gating" / "Pond State Variables" gain `changedF` and the pass rule
   (this is an authoritative-spec change); python-api.md gets `always_run` / `pond.skip()` /
   `pond.sources_changed()`; the orchestration concepts doc gets the quiet-interior Wave example.

---

## Deferred / open

- **Trickle empty-delta auto-report** (the remaining headline piece). A Trickle ripple whose composed
  delta is empty should report `changed=False` automatically, so a pure-Trickle pipeline goes quiet
  with zero API. The sound rule: a Pond auto-passes iff every output write this run went through a
  Trickle method *and* every such write was empty *and* no `write_table` (overwrite) happened — `True`
  otherwise (conservative; an overwrite can't be auto-detected, and a ripple that only does a side
  effect must not be assumed no-change). Implementation: have the Trickle writes (`merge_table` /
  `append_table` / `apply_zset` / the builder's `.merge()`/`.append()`) report per-write emptiness back
  through the Pond handle (a write-report callback alongside `skip_sink`), accumulate per-`f` in
  `DuckCore`, and fold into the `changed` decision next to the explicit skip. Reaches the same end as
  the explicit inlet-skip available today, minus the call.
- **UI surfacing** of pass vs idle vs running, and a "last changed" age distinct from "last run."
- **Spouts.** A Spout already only delivers when `sourceF > deliveredF`; with `changedF` it could also
  skip delivery when the Source merely republished unchanged. Natural extension once the core lands —
  the egress worker reads the Source's `changed_f` the same way a Sink does.
