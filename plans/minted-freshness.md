# Minted-freshness: demand carries an epoch; inlets stamp it

Status: **built** (engine + driver + routes + poller + tests, full suite green). Supersedes the
duct-solicit freshness discussion in cross-catchment-ducts.md.

## The model

Freshness `F` is a **demand epoch**, not "when the bytes were read." Every demand — pull *or*
push — carries a minted UTC timestamp `m`. An **inlet's run freshness = the max `m` of its
outstanding demand** (never `now`, never `targetF`, even though push `m == targetF` by construction).
Non-inlets stay source-derived (`min`/`max` of source `end_f`); draws stay `remote_f` (which now
equals the upstream inlet's `m`, mirrored). Windows and Force are documented exceptions.

This makes "a Pulse/Tap at `T` produces freshness `T` for everything it reaches" true *by
definition*, regardless of when each node physically runs (delayed sibling, or across a duct). It is
consistent with `F` already being logical (frozen + reused across crash-replay and immediate retries).

### Minting rules
- **Trigger** on a Pond P: `m = now`. Pull (tap/wave) sets the pull token with `m`; push
  (pulse/tide) adds target `now` with `m = now`.
- **Idle Pond receives pull (cold start): pass the incoming `m` through** to its sources unchanged —
  do *not* wait to start-and-mint (that delay desyncs the epoch). One epoch flows to all inlets.
- **Sustaining re-arm**: when a pull-driven Pond re-arms its sources *at its own start*
  (`start_pond_run`), the mint is **that Pond's start time** (`now`), not the downstream origin.
- **Inlet stamp**: `start_f = max(outstanding demand m)`, excluding the Force `NEVER` sentinel;
  fall back to `now` only for Force / when there is no real `m`.

### Why `m` is separate from `targetF`
For pulse/tide `m == targetF` always (both are `now` at fire). We still carry `m` explicitly and
stamp from it, so the rule is "freshness = the minted epoch" everywhere, with no reliance on the
push/pull coincidence.

## Diamond validation (A→B, A→C, B→D, C→D)
- Cold start mints `t0` at D, passes through B,C → A@`t0`; B,C@`t0`; D@`t0`. C slower only delays D.
- C optional: D runs on B at `t0`; C later stamps its epoch `t0` (not run-now) → C@`t0` = D@`t0`,
  no spurious edge. Edge case to test: D re-taps a new epoch while optional C still finishes the old.

## Implementation plan

### Phase 1 — Engine (`engine/core.py`, `engine/catchment.py`) — pure, test-first
1. State: add `pull_m: datetime` to `PondState` (the active pull's epoch). Push targets carry their
   `m`; since `m == targetF` for pulse/tide, represent push `m` by the target value but read inlet
   freshness through an `m`-named path (decision to finalize in code: parallel `target→m` map vs.
   treating `target_f` as `m`). Minting is Pond-level (ripples inherit via the run-start stamp).
2. Thread `m` through the propagation functions: `pond_receive_pull(..., m)`,
   `pond_set_has_pull(..., m)`, `pond_add_target(..., t, m)`. Trigger entry points (`tap_pond`,
   `pulse_pond`, etc.) mint `m = now`.
3. Cold-start pass-through vs. sustain mint: `pond_set_has_pull`'s source re-arm carries the
   receiving Pond's incoming `m` (pass-through); `start_pond_run`'s source re-arm carries the
   starting Pond's `now`.
4. Inlet stamp in `start_pond_run`: for a sourceless, windowless, non-draw Pond,
   `f = max(real demand m)` instead of `now`. Force (`NEVER` target) and windows unchanged.
5. Preserve the four landmines: gating still uses `target_f` (`source_f >= min_target`); only the
   *stamp* uses `m`. Cold-start `startF` guards, Tide clock ref, and the run-start ripple stamp
   (ripples still stamped with `pond.start_f = m`) all unchanged. Verify monotonicity (demands are
   only added when `> end_f`, and mints are `now`-increasing).
6. `test_engine`/`test_engine_split`: cold-start-tap simultaneity with staggered completions; the
   delayed-run case (stamp the epoch, not run-now); the diamond + optional-C variant. Update any
   existing sims that encoded run-now freshness.

### Phase 2 — Duct: forward demand with `m`
7. Poller: drop the blanket tap. Forward the draw's outstanding demand to the upstream **carrying
   `m`** — push targets as a pulse at `(target_f, m)`, pull as a tap carrying `m`.
8. Producer demand endpoint accepts a minted `m` (+ `target_f` for push) so the upstream mints the
   *same* epoch the downstream did. Then upstream inlet stamps `m` → `remote_f = m` → draw mirrors
   `m`. No draw-side freshness logic.
9. `Driver.draws()` / `duct_targets()` expose the draw's outstanding demand (targets+`m`, pull+`m`).

### Phase 3 — Provenance / Trickle decision
10. `F` is a pure coordination epoch. Document in python-api.md + the incremental guide that
    `pond.f` is the demand epoch, not "max data timestamp read"; a slow run can read data past its
    own `F`. If/when Trickle needs data-as-of, carry it **separately** from `F` (not part of this
    change).

### Close-out
`test_duct` should now show the drawn `products` and local `transactions` at the **same** `F` after a
pulse, with the spurious products→sales "updated" edge gone. `ruff check .`; frontend unaffected.
