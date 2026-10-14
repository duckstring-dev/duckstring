# Pulse throttle: pacing saturated push demand by the bottleneck

Status: **D1 to D4 built (engine, reload re-send, docs and CLAUDE.md); D5, the playground tour chapter, pending.** The reference implementation is
the playground's TypeScript engine on branch `playground-pulse-release` (`playground/src/lib/
orchestration.ts`, commit `58876400`), with its tests in `playground/tests/unit/throttle.spec.ts`. This
plan ports it to `engine/catchment.py` and the Driver. Per CLAUDE.md the Python engine is a
behaviour-for-behaviour port of the simulation; the playground already carries every rule below, so the
two describe one mechanism.

Settled with the author (2026-10-13):

- The throttle is **always on**. No setting, flag or switch.
- It is **silent** in Duckstring: no UI elements, status fields, metrics or logs for throttling or slow
  state. It is a protection against too-fast Pulses, not a feature to operate.
- Push propagates to **required** Sources (with the two exceptions below); the throttled flag never
  crosses an optional edge.
- A Pond that **unblocks** re-sends its unmet demand upstream.
- **Tap and Wave are untouched.** The Pulse changes are purely additive.

---

## The problem

A Pulse pushes a target freshness to the Outlet and, synchronously, to every Pond above it. When
Pulses arrive faster than the slowest stage can serve them (a burst of gets, or a Tide below the
bottleneck's period), every Pond upstream of the bottleneck runs once per Pulse, while the bottleneck
absorbs them several to a run. The work upstream is wasted, and a fast sibling path (a diamond, or a
second Inlet feeding a join) runs at the Pulse rate instead of the join's.

Pull doesn't over-run, because a node re-arms its parents only when it starts. But a Tap on a pipeline N
Ponds deep delivers data N cycles stale, and a Wave hands each stage a different generation, so data ages
one bottleneck period per stage. The throttle keeps Pulse semantics (a generation travels upstream to
downstream back to back, so it stays fresh) and adds Wave's pacing only when demand saturates.

## The mechanism

Detection is per Ripple, pacing per Pond, and one derived flag crosses a Pond boundary.

1. **Slow (Ripple).** A Ripple is slow once two demands reach its Pond during one of its runs: its run
   takes longer than the gap between them. It stays slow until it goes idle (finishes and doesn't
   immediately start again). An idle stage isn't the bottleneck: a stage below the real bottleneck idles
   every cycle waiting for it, and a stray flag from one long noisy run clears the same way.
2. **Throttled (Pond, downstream).** A Pond is throttled if it has a slow Ripple or a throttled
   **required** Source (with only optional Sources: if **all** of them are throttled, since it runs on
   whichever is fresher). Derived like `is_blocked`.
3. **Hold.** Some Ponds keep the demand that reaches them as their own targets (so they still run on
   anything fresher that arrives) without passing it upstream straight away:
   - a throttled **trigger Pond** (one a Pulse, Tide or duct Pulse targets: normally the Outlet) holds all
     its demand, and
   - any other Pond with a slow Ripple (the bottleneck) holds what reaches it.
4. **Release.** Each holding Pond sends one Pulse upstream, at `now`, per turn, while it has unmet demand:
   - a bottleneck's turn is all its slow Ripples having started a new run since its last send (the moment
     a Wave would re-arm), so requests travel upstream timed to the bottleneck's starts;
   - a trigger Pond's turn is completing a run (or ceasing to be throttled), so new demand enters at the
     rate the Outlet is served, which bounds every path into it, diamonds and siblings included. A Pond
     whose slow Ripple has gone idle while it still holds demand uses the same rule.

   A trigger Pond never paces on its own slow Ripple: it takes raw trigger demand, so under a fast trigger
   its own Ripple always looks slow, whether or not it is the bottleneck. Pacing on both schedules sends
   two Pulses per cycle.
5. **Inlet stamp.** An Inlet (not a Draw) doesn't start a Pond Run while any of its first Ripples (no
   intra-Pond parent) is busy. An Inlet stamps its freshness when its Pond Run starts but reads its data
   when its first Ripples run, so a run queued behind a busy first Ripple would be labelled up to a whole
   run older than its data. Later Ripples may still be busy; runs pipeline through them.
5a. **Release stamp** (Python only; settled 2026-10-13). A release is pacing and carries no epoch anyone
   asked for, so an Inlet a release reaches stamps its next Run's **start**, not the release time. Under
   the minted-freshness rule an Inlet stamps the newest epoch it was asked for, and a trigger Pond's
   release can reach a busy bottleneck Inlet a whole run before that Inlet can start: the slow-sibling
   topology read 12 s against a Wave's 8 s until this rule. A user's Pulse, Tide or duct epoch still
   stamps its minted epoch. The playground stamps every Inlet Run's start (it has no minted epochs), so
   this brings the two engines together.

Three fixes go with it and stand on their own (D1):

6. **A target a run in flight covers counts as met.** `pond_add_target` ignores `t <= start_f`. This is
   the latent repeat-epoch bug (`project_tide_fix_investigation`): a repeat request for an epoch a Pond is
   already running re-runs it, or through the no-change skip completes the in-flight run early as a pass.
   Releases make same-instant repeats routine.
7. **Push to required Sources.** See "Required and optional Sources".
8. **Re-send on unblock.** Demand is refused at a blocked Pond, so a Pulse forwarded while a Source was
   blocked is lost (true today, with or without the throttle). When a Pond unblocks, it forwards its newest
   unmet target to its push Sources.

Measured in the playground against a Wave on nine topologies (deep chain, long lead, decreasing chain,
demo, slow sibling, diamond, diamond with a long lead on the slow path, Outlet as the bottleneck, and the
same behind a long lead), with exact and drifting tick times: the same bottleneck and Outlet run counts,
upstream settling at one run per bottleneck run, and data at least as fresh (deep chain 11 s against
31 s). Sparse Pulses run every Pond once per Pulse. Under noisy run times it stays about twice as fresh as
a Wave but loses throughput on long leads (long lead at σ 0.5: 29 Outlet runs against 36) and runs
upstream more, because each time the bottleneck idles the throttle briefly lifts and refills the pipeline.

---

## Required and optional Sources

The orchestration spec ("Required and optional parents") says a push target propagates only to required
parents: forcing an optional parent to a target its Sink won't wait for is pointless. The engine
currently forwards push to every Source. The throttle depends on the distinction, and the author has
chosen to align push with the spec.

| Rule | Required Sources exist | Only optional Sources |
|---|---|---|
| Freshness (unchanged) | `min` over required | `max` over all |
| Push propagation (`push_sources`) | required, plus an **unbuilt** optional Source | all |
| Throttled from Sources | any required Source throttled | every Source throttled |
| Holding and release forward to | `push_sources` | `push_sources` |
| Blocked (unchanged) | any required Source blocked | not blocked by Sources |
| Pull (unchanged) | all Sources | all Sources |

**Unbuilt optional Source.** One that has never started a run (`start_f == NEVER`) and holds no push
target receives a Pulse from its Sink, once. Without this, a Sink whose optional Source has never run
reads an asset nobody builds (`MissingSourceAsset`, and the Sink parks as waiting) until some other path
happens to demand it. One Pulse is enough: after its first start, it falls back to the normal rule.

**What needs care, and the docs warning.** Under push alone, a Pond reachable from a trigger only through
optional edges runs only when something else demands it:

- another path on which it's a required Source;
- a trigger set on it directly (Pulse or Tide on any deployed Pond is already supported, so an optional
  Source, or a whole optional subgraph, gets its own cadence that way);
- pull: Tap and Wave still propagate to every Source, optional included, as today.

That asymmetry is deliberate for now (Tap and Wave are untouched), but it has to be written down. The
Orchestration concept page, Orchestration Theory and the Scheduling guide need a short warning: a Pulse
or Tide doesn't refresh an optional Source after its first build; give it its own Tide if it needs a
cadence. The changelog for the release flags the behaviour change.

Intra-Pond, optional parent Ripples need nothing: slowness is per Ripple, and "first Ripples" for the
Inlet stamp rule are those with no intra-Pond parent at all.

---

## State

All throttle state is **soft**: in memory on `PondState`, not persisted, rebuilt by observation. Losing
it (a restart) falls back to plain Pulse until two demands land in one run again. Held demand is ordinary
push targets, persisted in `pond_target` as today. No migration.

`engine/core.py`:

```python
@dataclass
class ThrottleState:
    counts: dict[RippleId, tuple[int, int]] = field(default_factory=dict)  # ripple -> (run, demands seen in it)
    slow: set[RippleId] = field(default_factory=set)
    held: int = 0                      # demands held since the last send
    sent_at: dict[RippleId, int] = field(default_factory=dict)  # each Ripple's runs_started at the last send
    trigger: bool = False              # a trigger targets this Pond (cleared when its demand drains)
    completed: bool = False            # completed a run since the last send
    released: bool = False             # Inlets: a release reached it, so its next Run stamps its start

@dataclass
class PondState:
    ...
    throttle: ThrottleState | None = None   # created on first use; deep-copied in PondState.copy()
```

`RippleState` already has `is_running` and `runs_started`, which is all detection and release need. Run
numbers, not timestamps, identify a Ripple's runs (a Ripple's runs never overlap). No durations are used.

## Engine changes (`engine/catchment.py`)

```python
def pond_add_target(s, pid, t, from_trigger=False, release=False):
    ps = s.pond_states[pid]
    if ps.is_blocked or t <= ps.end_f or t in ps.targets:
        return
    if t <= ps.start_f:                      # 6: a run in flight already covers it
        return
    ps.targets.append(t)
    if release and not s.ponds[pid].sources: # 5a: an Inlet stamps its next Run's start
        _throttle(s, pid).released = True
    if from_trigger:
        _throttle(s, pid).trigger = True
    if not ps.repairing:                     # (Force's NEVER never gets here: NEVER <= end_f)
        _count_demand(s, pid)
        if _holds(s, pid):
            _throttle(s, pid).held += 1
            return
    _forward(s, pid, t, release)

def push_sources(s, pid):                    # 7
    pond = s.ponds[pid]
    required = [sp for sp in pond.sources if sp not in pond.optional_sources]
    if not required:
        return list(pond.sources)
    def unbuilt(sp):
        sps = s.pond_states[sp]
        return sps.start_f == NEVER and not any(t > NEVER for t in sps.targets)
    return [sp for sp in pond.sources if sp not in pond.optional_sources or unbuilt(sp)]

def _forward(s, pid, t, release=False):
    th = _throttle(s, pid)
    th.held, th.completed = 0, False
    th.sent_at = {r: s.ripple_states[r].runs_started for r in ripples_of(s, pid)}
    for sp in push_sources(s, pid):
        pond_add_target(s, sp, t, release=release)

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

def is_throttled(s, pid):                    # 2: never across an optional edge
    th = s.pond_states[pid].throttle
    if th and th.slow:
        return True
    pond = s.ponds[pid]
    required = [sp for sp in pond.sources if sp not in pond.optional_sources]
    if required:
        return any(is_throttled(s, sp) for sp in required)
    return bool(pond.sources) and all(is_throttled(s, sp) for sp in pond.sources)

def _holds(s, pid):                          # 3
    pond, ps = s.ponds[pid], s.pond_states[pid]
    if not push_sources(s, pid) or pond.is_draw or pond.is_spout:
        return False
    th = ps.throttle
    return is_throttled(s, pid) if th.trigger else bool(th.slow)

def _release_due(s, pid):                    # 4
    th = s.pond_states[pid].throttle
    if th.slow and not th.trigger:
        return all(s.ripple_states[r].runs_started > th.sent_at.get(r, -1) for r in th.slow)
    return th.held > 0 and (th.completed or not is_throttled(s, pid))

def _settle_throttle(s, now) -> bool:        # inside sentinel, after the start fixpoint
    changed = False
    for pid, ps in s.pond_states.items():
        th = ps.throttle
        if th is None:
            continue
        idle = {r for r in th.slow if not s.ripple_states[r].is_running}
        if idle:                             # 1: slow clears on idle
            th.slow -= idle
            changed = True
        if not any(t > NEVER for t in ps.targets):
            th.trigger, th.held = False, 0   # demand drained
            continue
        if push_sources(s, pid) and _release_due(s, pid):
            _forward(s, pid, now, release=True)
            changed = True
    return changed
```

Wiring:

- **`sentinel`.** Wrap the existing start fixpoint in a loop that runs `_settle_throttle` after it and
  repeats while either changed (bounded; the playground uses 16 rounds). Sends are decided only after the
  starts: a slow Ripple that finished and restarted in the same instant must read as one turnover, not as
  idle (clear, release) and then turned over (release again).
- **`_complete_pond_run`** (real runs and passes): set `throttle.completed = True` if a `ThrottleState`
  exists.
- **Triggers.** `pulse_pond` and the Tide branch of `tick` call `pond_add_target(..., from_trigger=True)`.
  `Driver.pulse` (the API route, including duct Pulses) goes through `pulse_pond` already.
- **`can_start_pond`.** Rule 5: an Inlet that isn't a Draw doesn't start while a first Ripple is running.
- **`start_pond_run`.** Rule 5a: a non-windowed Inlet with `throttle.released` adds `now` to the epochs it
  stamps from (so it stamps its start) and clears the flag.
- **`derive_blocked`.** Rule 8: when `is_blocked` changes to false, `_forward(max(unmet finite targets))`
  before recursing to the Sinks.
- **`sleep_pond`, `kill_pond`, `clear_pond`.** Drop `ps.throttle`. `force_pond` and `refresh_pond` are
  unchanged.
- **`clone`/`copy`.** Deep-copy `ThrottleState`.
- `is_throttled` recurses like `derive_blocked`; graphs are small. Cache per `sentinel` pass only if
  profiling asks for it.

The Duck's `WorkerEngine` is unchanged: it pushes one Pond Run to completion and never sees demand.

## Special cases

- **Inlets** can be slow (their first Ripple runs while demand arrives) and so throttle everything
  downstream, but can't hold: they have no Sources. A Pulse's minted epoch is the max target at the
  Inlet's Pond start, so waiting for the first Ripple makes the stamp later and truer, never earlier; a
  release's is the start itself (rule 5a). Before detection (the startup) a user's Pulse can still reach a
  busy Inlet and stamp its own, older epoch, which is that Pulse's meaning.
- **Windowed Inlets.** Freshness is the window end, so rule 5 changes nothing material. Between windows the
  Inlet is idle, so any slow flag clears.
- **Multi-Ripple Ponds.** Slowness is per Ripple, and a bottleneck releases only when all its slow Ripples
  have started a new run, so the slowest sets the pace, wherever it sits in the Pond.
- **Trigger Ponds** (`trigger` set by Pulse, Tide or duct Pulse; cleared when their demand drains).
  Triggers belong on Outlets, but the routes accept any Pond, and the optional-Source guidance now relies
  on that. A trigger on a mid-pipeline Pond holds all its demand (its Sinks' and its trigger's) on the
  completion schedule while throttled: coarser than the bottleneck schedule for a Sink path it also
  bottlenecks, but bounded and never doubled.
- **Tap, Wave, Wake (pull).** Untouched. The throttle gates only push forwarding. A Wave and a Tide on
  separate Outlets sharing upstream still run it at the union of their demand, as today.
- **Tide.** Demand at a throttled Outlet is still added to its targets, so the Tide clock (`max_target`
  or `start_f`) and `next_wake` are unchanged. A Tide with bound 0 behaves like a Wave (same run counts on
  every simulated topology).
- **Force, Refresh, Repair.** Force's `NEVER` target is neither counted nor held, and `force_pond` doesn't
  propagate. Refresh adds no demand. A Pond with `repairing` set neither holds nor counts: the Driver
  sequences repair and blocks normal demand there already.
- **Blocked, failed, killed, missing asset, `remote_down`.** A blocked Pond refuses demand, as today; rule
  8 re-sends what was refused once it unblocks (`clear`, recovery, redeploy, a Draw's upstream returning).
  A failed or killed Pond's Ripples aren't running, so its slow flags clear. Retry-on-change is unaffected.
- **No-change passes.** A pass starts no Ripples, so it neither counts demand nor turns a bottleneck over,
  and its Ripples aren't running, so slow flags clear. It does complete the Pond, so it counts for a
  trigger Pond's schedule.
- **Draws (downstream side of a duct).** No local Sources, so a Draw never holds. Its transfer Ripple can be
  slow, which correctly throttles what's downstream of a slow transfer. `Driver.draws()` forwards the
  Draw's max target once per change, so demand crossing the duct is already paced by the downstream
  Catchment.
- **Ducts (upstream side).** A duct Pulse arrives through `Driver.pulse(at=...)`, so the exported Pond is
  a trigger Pond in the upstream Catchment and paces on its completions. Each Catchment throttles its own
  graph; only demand crosses the duct. Rule 6 makes the poller's re-send guard (`solicited`) a safety net
  rather than load-bearing.
- **Spouts.** Pull only (a standing Wake), never push. Unchanged.
- **Multiple majors.** Everything is keyed by pond key `name@major`. Nothing special.
- **Persist and Pools.** `source_visible_f` gates reads; the throttle reads run counts and demand
  arrivals, never freshness. Unchanged.
- **Modelled versus real Ripple runs.** The Catchment models Ripple starts in `sentinel` and learns
  completions from the Duck's `ripple` events, so `is_running` also spans the Duck's queueing. Detection
  and release use the model consistently, as the simulation does.
- **Restart.** Throttle state is dropped, so the engine is plain Pulse until re-detection. Held demand sits
  in `pond_target` without having been forwarded, so `Driver.reload` re-sends once: for every Pond with an
  unmet target, `_forward(max(targets))`. Rule 6 and the existing de-duplication keep this to at most one
  extra upstream run per path. `resume_incomplete` is unchanged.
- **Liveness.** Rule 5 can delay an Inlet's `BeginRun`; `_check_liveness` judges in-flight runs by
  `start_f > end_f`, which a delayed start doesn't create. Unchanged.

## Driver

Only two changes, both silent:

- `Driver.reload`: the restart re-send above, after state is restored and before the first `_process`
  (as built: `engine.resend_held_demand`, skipping blocked Ponds, which solicit nothing).
- Nothing else: no setting, no status or metrics fields, no logs. The Web UI and CLI are unchanged.

## Docs

- `orchestration_theory.md`: a section after push and pull, "Saturated push: the throttle", in the problem,
  need, concept, mechanism order; the Pond State Variables pseudocode gains the throttle, rules 6 to 8 and
  `push_sources`; the guidance "Reach for pull when consumption is frequent" changes, since push no longer
  over-runs; the optional-parent section gains the warning above and the unbuilt-Source exception.
- Concepts orchestration page and the Scheduling guide: what changes for a Pulse or Tide under
  saturation, in plain terms; the optional-Source warning; link the playground.
- CLAUDE.md: the orchestration model's subtle rules gain "slow clears on idle" and "sends are decided
  after starts"; Required/optional push semantics.

The playground tour gets a chapter on the throttle as a separate piece of work.

---

## Deliverables (ship order)

- **D1. Engine fixes, independent of the throttle** (rules 6 to 8). `pond_add_target` ignores
  `t <= start_f`; `push_sources` (required, unbuilt optional, or all); `derive_blocked` re-sends on unblock.
  Tests below. Changelog entry and docs warning for the optional-Source change.
- **D2. The throttle in the engine.** `ThrottleState`, the functions above, the `sentinel` settle loop, the
  completion flag, trigger plumbing, rule 5, resets, `clone`/`copy`.
- **D3. Driver reload re-send.** `tests/test_restart.py`.
- **D4. Docs and CLAUDE.md.**
- **D5. Playground tour chapter** (separate).

## Tests (Python)

Port `playground/tests/unit/throttle.spec.ts` to `tests/test_engine.py` using its `Driver` harness:

- **Saturation.** The nine topologies above, under Pulses every 1 s, a 0 s Tide, and a 1 s Tide with
  drifting ticks (extend the harness with a seeded drift of −10 to +29 ms per 100 ms step, as the
  playground's real-time ticks drift). For each: bottleneck and Outlet runs equal a Wave's; over the
  second half, upstream runs at most one per bottleneck run and data age at most a Wave's (age over the
  whole run includes the startup, where a user's Pulse at a busy Inlet keeps its minted epoch).
- **Sparse.** Pulses every 60 s run every Pond exactly once per Pulse.
- **Optional Sources.** A slow optional Source neither throttles its Sink nor is asked again after its
  first build (one run under Pulses every 1 s); a Pond with only optional Sources is throttled only when
  all of them are; an unbuilt optional Source gets exactly one Pulse.
- **Rule 6.** A repeat Pulse for the epoch a Pond is running neither re-runs it nor completes it early as a
  pass (the regression the repeat-epoch bug caused through the no-change skip).
- **Rule 8.** Fail a Source mid-chain while demand is pending, `clear_pond` it (which unblocks without
  running), and the pending demand reaches the Inlet and the Outlet is served. Without rule 8 it strands.
- **Interplay.** Blocked and failed Ponds; Sleep, Kill and Clear drop throttle state; Force and repair are
  unaffected; a pass counts as an Outlet completion.
- **Restart** (`tests/test_restart.py`): held demand survives a restart and is re-sent once.
- **Ducts** (`tests/test_duct.py`): a duct Pulse paces the upstream Catchment.
- **Gates.** `test_engine`, `test_restart`, `test_no_change`, `test_duct` and `test_runtime`'s demo chains
  pass, and CLAUDE.md's subtle orchestration rules still hold. Existing tests that expect push to reach an
  optional Source change with D1; update them deliberately, not incidentally.

A cross-check is cheap and worth having: run the same deterministic scenario through the TypeScript and
Python engines and compare per-Pond run counts and completion times.

## Refinements (2026-10-14)

Adopted after testing (`bench/continuous_refresh/`, `bench/sibling_alignment/`); in both engines, with the
Node cross-check covering them.

- **Sibling alignment.** A Pond whose required push Sources are partly throttled sends a target to the
  throttled ones now and holds it back, per Source, from the others until each throttled one starts a new Run
  (its turn). A held-back Source that becomes throttled itself gets its target at once. The faster Sources then
  read with the slow one, so the join's freshness isn't the older of two stamps that drifted apart (slow sibling
  Inlet: 7 to 13% staler than a Wave under noise before). Releasing on the throttled Source's next start, not
  once it starts a Run at or past the target, keeps a shared upstream fed (the diamond with a long lead starved
  under the stricter rule).
- **The floating clock counts from requests.** A Tide's reference is its newest trigger request (pending or
  served; `PondState.last_request`, soft), else `start_f`. Counting from the freshness a request was served with
  stretched a Tide beside a busier Outlet on shared upstream (10 s to 12 s), since that freshness runs ahead of
  the request.
- **Sleep re-sends other Ponds' demand** to the Sources it cleared (only to them, so it doesn't count as a
  release anywhere else). Before, Sleep stranded a pending request another Pond had forwarded to a slept Source.

## Known limits

- **A Tide beside a saturating neighbour** (or with the bottleneck several stages up) can run slower than its
  target: a throttled trigger Pond releases on its completions, so the stages between it and the bottleneck carry
  one of its requests at a time. About 5% of Tides in random multi-Outlet graphs (p99 1.5 times, worst 2.3 times,
  at Tides of 2 to 30 s); bounded by that lead time, so negligible when the period is much longer than the
  pipeline. A release turn on the trigger Pond's Sources' starts was tried and rejected (it over-released under
  the trigger's own saturation); the bottleneck's own turn, passed downstream like the throttled flag, would be
  the signal to use if this is ever worth fixing.

- **Startup.** Nothing is detected until the bottleneck has started a run, so the first lead time runs as
  plain Pulse: roughly one extra upstream run per Pulse for the length of the lead.
- **Knife edge.** When the stages above the bottleneck take about as long as it does, small timing
  differences decide whether one or two generations circulate. With two, the data is one bottleneck run
  staler but stable (deep chain with drift: 16 s, against a Wave's 31 s).
- **Noisy run times.** The pipeline carries little spare work, so variable durations starve the bottleneck
  more than under a Wave, whose every stage holds a generation. Measured: fresher than a Wave throughout,
  throughput lower on long leads, upstream runs higher.
