# Pulse throttle: pacing saturated push demand by the bottleneck

Status: **designed and simulated; not built in the Python engine.** The reference implementation is
the playground's TypeScript engine on branch `playground-pulse-release` (`playground/src/lib/
orchestration.ts`, behind `pulseThrottle`; commit `0228dda6`), with its tests in
`playground/tests/unit/throttle.spec.ts`. This plan ports it to `engine/catchment.py`, wires it through
the Driver, and settles the special cases the playground doesn't model (required and optional Sources,
ducts, restarts, failures, repair). Per CLAUDE.md the Python engine is a behaviour-for-behaviour port of
the simulation, so where this plan changes a rule, the playground changes with it (D6).

---

## The problem

A Pulse pushes a target freshness to the Outlet and, synchronously, to every Pond above it. When
Pulses arrive faster than the slowest stage can serve them (a burst of gets, or a Tide below the
bottleneck's period), every Pond upstream of the bottleneck runs once per Pulse, while the bottleneck
absorbs them several to a run. The work upstream of the bottleneck is wasted, and a fast sibling path
(a diamond, or a second Inlet feeding a join) runs at the Pulse rate instead of the join's.

Pull (Tap/Wave) doesn't over-run, because a node re-arms its parents only when it starts. But a Tap
on a pipeline N Ponds deep delivers data N cycles stale, and a Wave hands each stage a different
generation, so every stage starts together and data ages one bottleneck period per stage.

The throttle keeps Pulse semantics (each run is driven by a target, so a generation travels upstream
to downstream back to back, fresh) and adds Wave's pacing only when demand saturates. Sparse demand
is untouched.

## The mechanism

Detection happens per Ripple, pacing per Pond, and only one flag crosses a Pond boundary.

1. **Slow (Ripple).** A Ripple is slow once two demands reach its Pond during one of its runs: its run
   takes longer than the gap between them. It stays slow until it goes idle (finishes and doesn't
   immediately start again). An idle stage isn't the bottleneck. A stage below the real bottleneck
   idles every cycle waiting for it, and a stray flag from one long noisy run clears the same way.
2. **Throttled (Pond, downstream).** A Pond is throttled if it has a slow Ripple, or if its Sources are
   throttled (see "Required and optional Sources" for exactly which). Like `is_blocked`, it is derived
   from a Pond's own state and its Sources'.
3. **Hold.** Some Ponds keep the demand that reaches them as their own targets (so they still run on
   anything fresher that arrives) but don't pass it upstream straight away:
   - a Pond with a slow Ripple (the bottleneck), and
   - a throttled **trigger Pond** (one a Pulse, Tide or duct Pulse targets: normally the Outlet).
4. **Release.** Each holding Pond sends one Pulse upstream, at `now`, per turn, while it has unmet
   demand:
   - a bottleneck's turn is all its slow Ripples having started a new run since its last send (the
     moment a Wave would re-arm), so requests travel upstream timed to the bottleneck's starts;
   - a trigger Pond's turn is completing a run, so new demand enters the pipeline at the rate the
     Outlet is served, which bounds every path into it, diamonds and siblings included.

   A trigger Pond never paces on its own slow Ripple: it takes raw trigger demand, so under a fast
   trigger its own Ripple always looks slow whether or not it is the bottleneck. Pacing on both
   schedules sent two Pulses per cycle.
5. **Inlet stamp.** While the throttle is on, an Inlet doesn't start a Pond Run while any of its first
   Ripples (no intra-Pond parent) is busy. An Inlet stamps its freshness when its Pond Run starts but
   reads its data when its first Ripples run, so a run queued behind a busy first Ripple would be
   labelled up to a whole run older than its data. Later Ripples may still be busy; runs pipeline
   through them.
6. **A target a run in flight covers counts as met.** `pond_add_target` ignores `t <= start_f`. This is
   the latent repeat-epoch bug (`project_tide_fix_investigation`): a repeat request for an epoch a Pond
   is already running re-runs it (or, through the no-change skip, completes the in-flight run early as
   a pass). Releases make same-instant repeats routine, so it must be fixed with this. It is a
   correctness fix in its own right, so it applies whether or not the throttle is on (D1).

Measured in the playground against a Wave on nine topologies (deep chain, long lead, decreasing chain,
demo, slow sibling, diamond, diamond with a long lead on the slow path, Outlet as the bottleneck, and
the same behind a long lead), with exact and drifting tick times: the same bottleneck and Outlet run
counts, upstream settling at one run per bottleneck run, and data at least as fresh (deep chain 11 s
against 31 s). Under noisy run times it stays about twice as fresh as a Wave, but loses throughput on
long leads (long lead at σ 0.5: 29 Outlet runs against 36) and runs upstream more (each time the
bottleneck idles, the throttle briefly lifts and refills the pipeline).

---

## State

All throttle state is **soft**: in memory on `PondState`, not persisted, rebuilt by observation.
Losing it (a restart) falls back to plain Pulse until two demands land in one run again. Held demand
itself is ordinary push targets, which are persisted (`pond_target`) as today.

`engine/core.py`:

```python
@dataclass
class ThrottleState:
    counts: dict[RippleId, tuple[int, int]] = field(default_factory=dict)  # ripple -> (run, demands seen in it)
    slow: set[RippleId] = field(default_factory=set)
    held: int = 0                      # demands held since the last send (display and metrics only)
    sent_at: dict[RippleId, int] = field(default_factory=dict)  # each Ripple's runs_started at the last send
    trigger: bool = False              # a trigger targets this Pond: when throttled it paces on completion
    completed: bool = False            # completed a run since the last send (trigger schedule)

@dataclass
class PondState:
    ...
    throttle: ThrottleState | None = None   # created on first use; copied in PondState.copy()

@dataclass
class EngineState:
    ...
    pulse_throttle: bool = False       # the feature flag (D3); carried by clone()
```

`RippleState` already has `is_running` and `runs_started`, which is all detection and release need.
Run numbers, not timestamps, identify a Ripple's runs (a Ripple's runs never overlap). No durations
are used anywhere.

## Engine changes (`engine/catchment.py`)

Pseudocode against the existing functions. `required(s, P)` is the set push propagates to (see the
next section).

```python
def pond_add_target(s, pid, t, from_trigger=False):
    ps = s.pond_states[pid]
    if ps.is_blocked or t <= ps.end_f or t in ps.targets:
        return
    if t <= ps.start_f:                      # D1: a run in flight already covers it
        return
    ps.targets.append(t)
    if s.pulse_throttle and t > NEVER:       # Force's NEVER sentinel is not demand
        _count_demand(s, pid)
        if from_trigger:
            _throttle(s, pid).trigger = True
        if _holds(s, pid):
            _throttle(s, pid).held += 1
            return
    _forward(s, pid, t)

def _forward(s, pid, t):
    if s.pulse_throttle:
        th = _throttle(s, pid)
        th.held = 0
        th.completed = False
        th.sent_at = {r: s.ripple_states[r].runs_started for r in ripples_of(s, pid)}
    for sp in push_sources(s, pid):          # required Sources, or all if none is required
        pond_add_target(s, sp, t)

def _count_demand(s, pid):
    th = _throttle(s, pid)
    for r in ripples_of(s, pid):
        rs = s.ripple_states[r]
        if not rs.is_running:
            continue
        run, n = th.counts.get(r, (rs.runs_started, 0))
        n = n + 1 if run == rs.runs_started else 1
        th.counts[r] = (rs.runs_started, n)
        if n >= 2:
            th.slow.add(r)

def _holds(s, pid):
    pond, ps, th = s.ponds[pid], s.pond_states[pid], s.pond_states[pid].throttle
    if not push_sources(s, pid) or pond.is_draw or pond.is_spout or ps.repairing:
        return False
    if th.trigger:
        return is_throttled(s, pid)          # a trigger Pond holds all its demand while throttled
    return bool(th.slow)

def _settle_throttle(s, now) -> bool:        # called inside sentinel, after the start fixpoint
    changed = False
    for pid, ps in s.pond_states.items():
        th = ps.throttle
        if th is None:
            continue
        idle = {r for r in th.slow if not s.ripple_states[r].is_running}
        if idle:                             # slow clears on idle
            th.slow -= idle
            changed = True
        if not any(t > NEVER for t in ps.targets):
            th.trigger = False               # demand drained: no longer a trigger Pond
            continue
        if _release_due(s, pid):
            _forward(s, pid, now)
            changed = True
    return changed

def _release_due(s, pid):
    th = s.pond_states[pid].throttle
    if not s.pulse_throttle:
        return th.held > 0                   # throttle switched off: flush
    if th.slow and not th.trigger:           # bottleneck schedule
        return all(s.ripple_states[r].runs_started > th.sent_at.get(r, -1) for r in th.slow)
    return th.held > 0 and (th.completed or not is_throttled(s, pid))   # completion schedule
```

Wiring:

- `sentinel`: wrap the existing start fixpoint in a loop that runs `_settle_throttle` after it and
  repeats while either changed (bounded, as in the playground's 16 rounds). Sends are decided only
  after the starts: a slow Ripple that finished and restarted in the same instant must read as one
  turnover, not as idle (clear, release) and then turned over (release again).
- `_complete_pond_run` (real runs **and** passes): set `throttle.completed = True`.
- `pulse_pond` and the Tide branch of `tick`: call `pond_add_target(..., from_trigger=True)`.
- `can_start_pond`: with the throttle on, an Inlet that isn't a Draw doesn't start while any first
  Ripple is running (rule 5).
- `sleep_pond`, `kill_pond`, `clear_pond`: drop `ps.throttle` (Sleep and Kill already clear the
  targets that held demand lived in). `force_pond`, `refresh_pond`: no change.
- `is_throttled(s, pid)`: own `throttle.slow`, or the Source rule below. Compute it on demand with the
  same recursion `derive_blocked` uses; graphs are small. If profiling ever says otherwise, cache it per
  `sentinel` pass.

## Required and optional Sources

The orchestration spec (`orchestration_theory.md`, "Required and optional parents") says a push target
propagates only to **required** parents: there is no point forcing an optional parent to a target the
node won't wait for. The engine today forwards to **every** Source, required or optional (and the
playground does too). The throttle makes this matter, because a slow optional Source would otherwise
throttle and pace a path it never gates.

| Rule | Required Sources exist | Only optional Sources |
|---|---|---|
| Freshness (unchanged) | `min` over required | `max` over all |
| Push propagation (`push_sources`) | required only | all (best effort: otherwise the Pond could never reach a target) |
| Throttled from Sources | **any** required Source throttled | **every** Source throttled (it runs on the fastest) |
| Holding and release forward to | `push_sources` | `push_sources` |
| Blocked (unchanged) | any required Source blocked | not blocked by Sources |

So an optional Source is off the throttle's critical path in every sense: it isn't asked, can't
throttle its Sink, and runs only as often as some other demanded path asks it. Aligning push
propagation with the spec is a behaviour change on its own (an optional Source stops receiving push
demand through this edge), so it ships as D1, flagged in the changelog, before the throttle.

Intra-Pond, optional parent Ripples need nothing: slowness is per Ripple, and "first Ripples" for the
Inlet stamp rule are those with no intra-Pond parent at all, optional or not.

## Special cases

- **Inlets.** Can be slow (their first Ripple runs while demand arrives) and so throttle everything
  downstream, but can't hold: they have no Sources. Rule 5 applies. A Pulse's minted epoch is the max
  target at the Inlet's Pond start, so waiting for the first Ripple makes the stamp later and truer,
  never earlier.
- **Windowed Inlets.** Freshness is the window end, not a minted epoch, so rule 5 changes nothing
  material; it applies harmlessly. Between windows the Inlet is idle, so any slow flag clears.
- **Multi-Ripple Ponds.** Slowness is per Ripple, and a bottleneck releases only when **all** its slow
  Ripples have started a new run, so the slowest sets the pace. A slow Ripple deep inside a Pond is
  seen the same way as a single-Ripple bottleneck.
- **Trigger Ponds.** `Driver.pulse` (the API route and duct Pulses), `pulse_pond`, and Tide emissions
  set `trigger`. The flag clears when the Pond's demand drains. Triggers belong on Outlets (docs
  convention), but the routes accept any Pond: a trigger on a mid-pipeline Pond holds **all** its demand
  (its Sinks' and its trigger's) on the completion schedule while throttled. That is coarser than the
  bottleneck schedule for any Sink path it also bottlenecks, but bounded and never doubled. (The
  playground holds only trigger demand there; align it, D6.)
- **Wave and Tap (pull).** Untouched. The throttle gates only push forwarding; `has_pull` re-arming,
  `restore_demand` and Wake are unchanged. A Wave and a Tide on separate Outlets sharing upstream still
  run it at the union of their demand, as today; aligned Tides remain the answer to that.
- **Tide.** Demand at a throttled Outlet is still added to its targets, so the Tide clock's reference
  (`max_target(targets) or start_f`) and `next_wake` are unchanged. A Tide with bound 0 under the
  throttle behaves like a Wave (same run counts on every simulated topology).
- **Force, Refresh, Repair.** Force's `NEVER` target is excluded from counting and holding, and
  `force_pond` doesn't propagate anyway. Refresh is lazy and adds no demand. A Pond with `repairing`
  set neither holds nor counts: the Driver sequences repair, and normal demand is blocked there already.
- **Blocked, failed, killed, missing asset, `remote_down`.** `pond_add_target` already refuses demand
  into a blocked Pond, so a release forwarded into a blocked Source is dropped, exactly as a plain Pulse
  is today (the existing "Pulse hit a blocked Source" strand, healed by solicitation). A failed or killed
  Pond's Ripples aren't running, so its slow flags clear. A retry-on-change run is unaffected.
- **No-change passes.** A pass starts no Ripples, so it neither counts demand nor turns a bottleneck
  over; its Ripples aren't running, so slow flags clear. A pass **does** complete the Pond, so it counts
  as a completion for a trigger Pond's schedule.
- **Draws (ducts, downstream side).** A Draw has no local Sources, so it never holds. Its single
  transfer Ripple can be slow, which correctly throttles everything downstream of a slow transfer.
  `Driver.draws()` forwards the Draw's max target once per change (the poller's `solicited`), so demand
  crossing the duct is already paced by the downstream Catchment's throttle.
- **Ducts (upstream side).** A duct Pulse arrives through `Driver.pulse(at=...)`, so the exported Pond
  is a trigger Pond in the upstream Catchment and paces on its completions. Each Catchment throttles
  its own graph; nothing crosses the duct but demand. D1's `t <= start_f` rule makes the poller's
  re-send guard belt-and-braces rather than load-bearing.
- **Spouts.** Pull only (a standing Wake); they never receive push demand. Unchanged.
- **Multiple majors.** Everything is keyed by pond key `name@major` and Sources resolve to the pinned
  major already. Nothing special.
- **Persist and Pools.** `source_visible_f` gates when a Sink can read; the throttle doesn't read
  freshness at all, only run counts and demand arrivals. Unchanged.
- **Modelled versus real Ripple runs.** The Catchment models Ripple starts in `sentinel` and learns
  completions from the Duck's `ripple` events, so `is_running` spans the Duck's queueing as well as the
  run. Detection and release use the model consistently, which is what the simulation did.
- **Restart.** Throttle state is dropped (D2), so the engine is plain Pulse until re-detection. But held
  demand sits in `pond_target` without ever having been forwarded. `Driver.reload` therefore
  re-propagates once: for every Pond with an unmet target, `_forward(max(targets))` to its push Sources.
  `t <= start_f` and the existing de-duplication keep this to at most one extra upstream run per path.
  `resume_incomplete` is unchanged.
- **Liveness.** Rule 5 can delay an Inlet's `BeginRun`; `_check_liveness` judges in-flight runs by
  `start_f > end_f`, which a delayed start doesn't create. Unchanged.

## Driver, API and UI

- **Flag.** `catchment_setting` key `pulse_throttle` (`on`/`off`), read into `EngineState.pulse_throttle`
  at boot and on change. `GET/PUT /api/catchment/settings` and `duckstring catchment settings
  --pulse-throttle on|off` (full access, like the other settings). No migration: the table exists.
- **`/api/status`.** Per Pond: `throttle: {throttled, slow: [ripple names], held, trigger}` when the
  flag is on. Per Ripple: `slow: bool`.
- **Web UI (`frontend/`).** The playground's badges on `PondNode` (`slow`, `throttled`, `holding N`)
  and the Sidebar note; colours from `store.ts` (pull amber, as in the playground).
- **Metrics.** `duckstring_pond_throttled` and `duckstring_pond_slow_ripples` gauges per Pond.
- **CLI.** `duckstring status` shows a throttled marker on the Pond line.
- **Logs.** Debug-level Driver log on slow set or cleared, hold and release, for diagnosing pacing.

## Docs

- `orchestration_theory.md`: a new section after push and pull, "Saturated push: the throttle", in the
  problem, need, concept, mechanism order; the Pond State Variables pseudocode gains the throttle; the
  "Reach for pull when consumption is frequent" guidance changes, since push no longer over-runs; the
  optional-parent propagation text stays (D1 makes the engine match it).
- Concepts orchestration page and the Scheduling guide: what changes for a Pulse or Tide under
  saturation, in plain terms; link the playground's Throttle toggle.
- `docs/docs/reference/cli/` and the HTTP API route table for the setting.
- CLAUDE.md: the orchestration model's rules gain the throttle (and the four subtle rules become six:
  slow clears on idle; sends are decided after starts).

---

## Deliverables (ship order)

- **D1. Engine fixes, independent of the throttle.** `pond_add_target` ignores `t <= start_f`; push
  propagates to required Sources only (all, when none is required). Tests in `tests/test_engine.py`
  for both, including the premature-pass case the repeat-epoch bug caused. Changelog entry for the
  optional-Source behaviour change.
- **D2. The throttle in the engine.** `ThrottleState`, the functions above, `sentinel` settle loop,
  `_complete_pond_run` flag, trigger plumbing in `pulse_pond` and `tick`, rule 5 in `can_start_pond`,
  resets in `sleep_pond`/`kill_pond`/`clear_pond`, `clone`/`copy`. Off by default in the engine.
- **D3. Driver.** The setting and its plumbing, `Driver.pulse` as trigger demand, reload
  re-propagation, status fields, metrics, CLI. `tests/test_restart.py` (held demand survives a restart
  and is re-propagated once) and `tests/test_duct.py` (a duct Pulse paces the upstream Catchment).
- **D4. UI.** Badges and the Sidebar note.
- **D5. Docs and CLAUDE.md.**
- **D6. Playground parity.** Port D1 to `orchestration.ts` (required-only propagation is new there;
  `t <= start_f` becomes unconditional); a trigger Pond holds all its demand while throttled; tests.
  Then the playground and the Python engine describe one mechanism again.
- **D7. Default on**, after a release with the setting available, if nothing regresses.

## Tests (Python)

Port `playground/tests/unit/throttle.spec.ts` to `tests/test_engine.py` using its `Driver` harness:

- The nine topologies above, under Pulses every 1 s, a 0 s Tide and a 1 s Tide with drifting ticks
  (extend the harness with a seeded drift of −10 to +29 ms per 100 ms step, as the playground's real-time
  ticks drift). For each: bottleneck and Outlet runs equal a Wave's; over the second half, upstream
  runs at most one per bottleneck run; data age at most a Wave's.
- Sparse Pulses (every 60 s) produce exactly the plain-Pulse result.
- Plain Pulse over-runs a diamond's fast path; the throttle doesn't.
- Switching the flag off mid-run flushes held demand.
- Required and optional: a slow optional Source neither throttles its Sink nor receives its push
  demand; a Pond with only optional Sources is throttled only when all of them are.
- Blocked and failed interplay; Sleep, Kill and Clear drop throttle state; Force and repair unaffected.
- A pass counts as an Outlet completion.
- The existing behaviour gates (`test_engine`, `test_restart`, `test_no_change`, `test_duct`,
  `test_runtime`'s demo chains) pass unchanged with the flag off, and with it on (the subtle rules in
  CLAUDE.md's orchestration model must still hold).

A cross-check is cheap and worth having: run the same deterministic scenario through the TypeScript and
Python engines and compare per-Pond run counts and completion times.

## Open decisions (for the author)

1. **Required-only push propagation (D1).** It matches the spec and the throttle needs it, but it is a
   behaviour change for anyone relying on a Pulse refreshing an optional Source. Ship it, or keep
   forwarding to optional Sources and only exclude them from throttling?
2. **Default.** Off behind the setting for a release (D7), or on from the start? Sparse demand is
   unchanged by construction, so the risk is confined to saturated Pulses and Tides.
3. **Wave.** A Tide with bound 0 under the throttle matches a Wave's run counts and is fresher on
   chains with fast stages above the bottleneck. Whether Tap and Wave stay, or become sugar over Pulse
   and Tide, is a separate decision; this plan leaves them untouched.
4. **Unblock re-propagation.** A Pulse forwarded into a blocked Source is lost today, with or without
   the throttle. The reload re-propagation helper could also run when a Pond unblocks. Worth doing now,
   or leave the existing behaviour?

## Known limits

- **Startup.** Nothing can be detected until the bottleneck has started a run, so the first lead time
  runs as plain Pulse: roughly one extra upstream run per Pulse for the length of the lead.
- **Knife edge.** When the stages above the bottleneck take about as long as it does, small timing
  differences decide whether one or two generations circulate. With two, the data is one bottleneck run
  staler but stable (deep chain with drift: 16 s, against a Wave's 31 s).
- **Noisy run times.** The pipeline carries little spare work, so variable durations starve the
  bottleneck more than under a Wave, whose every stage holds a generation. Measured: fresher than a
  Wave throughout, throughput lower on long leads, upstream runs higher.
