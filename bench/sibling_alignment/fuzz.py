"""Random DAGs, noise, triggers and interventions; then clear every halt, stop the triggers, and check that
all demand drains (nothing stranded, nothing running, no held-back target left)."""
import json, random, sys
from multiprocessing import Pool
sys.path.insert(0, "bench/continuous_refresh"); sys.path.insert(0, "tests")
import py_measure as pm
import test_engine as te
from duckstring.engine import NEVER, Trigger, catchment, clear_pond, kill_pond, pulse_pond, sleep_pond, wake_pond, force_pond

def make(rng):
    n = rng.randint(3, 10); spec = []
    for i in range(n):
        k = rng.choice([0, 1, 1, 2, 2, 3]) if i else 0
        srcs = sorted(rng.sample([f"P{j}" for j in range(i)], min(k, i)))
        secs = [round(rng.uniform(0.3, 6), 1) for _ in range(rng.choice([1, 1, 1, 2, 3]))]
        spec.append((f"P{i}", secs if len(secs) > 1 else secs[0], srcs))
    s, dur = te.graph(spec)
    for pid, p in s.ponds.items():
        if len(p.sources) > 1 and rng.random() < 0.2:
            p.optional_sources = {rng.choice(p.sources)}
    return s, dur

def run(args):
    case, align = args
    rng = random.Random(case); catchment._ALIGN = catchment._SOURCE_TURN = catchment._EVERY_FROM_REQUEST = align
    s, dur = make(rng)
    sinks = {sp for p in s.ponds.values() for sp in p.sources}
    outlets = [p for p in s.ponds if p not in sinks]
    pulses = {}
    for o in outlets:
        kind = rng.choice(["cont", "tide", "pulse", "none"])
        if kind == "cont": s.triggers[o] = Trigger(o, "tide", te.secs(0))
        elif kind == "tide": s.triggers[o] = Trigger(o, "tide", te.secs(rng.choice([1, 3, 10, 30])))
        elif kind == "pulse": pulses[o] = rng.choice([0.5, 1, 2, 7, 20])
    sigma = rng.choice([0, 0.25, 0.5, 1.0])
    d = pm.Noisy(s, dur, case, sigma)
    for i in range(3000):                       # 300 s with triggers and interventions
        for o, every in pulses.items():
            if i % max(1, round(every * 10)) == 0:
                d.state = pulse_pond(d.state, o, d.now)
        if rng.random() < 0.004:
            p = rng.choice(list(s.ponds)); act = rng.choice(["kill", "clear", "sleep", "wake", "force", "fail"])
            if act == "kill":
                d.state = kill_pond(d.state, p, d.now)
                for r in te.__dict__.get("_", []) or []: pass
                for rid in [r for r in d.inflight if d.state.ripples[r].pond_id == p]: del d.inflight[rid]
            elif act == "clear": d.state = clear_pond(d.state, p, d.now)
            elif act == "sleep": d.state = sleep_pond(d.state, p, d.now, upstream=rng.random() < 0.5)
            elif act == "wake": d.state = wake_pond(d.state, p, d.now)
            elif act == "force": d.state = force_pond(d.state, p, d.now)
            elif act == "fail":
                running = [r for r in d.inflight if d.state.ripples[r].pond_id == p]
                if running: d.fail_next(running[0])
        d.advance(); d.process()
    # drain: stop everything, clear every halt, and wait
    d.state.triggers = {}; d.fail_counts = {}
    for p in s.ponds:
        if d.state.pond_states[p].is_failed or d.state.pond_states[p].is_killed:
            d.state = clear_pond(d.state, p, d.now)
    for _ in range(6000):
        d.advance(); d.process()
    bad = []
    for p, ps in d.state.pond_states.items():
        if any(t > NEVER for t in ps.targets): bad.append((p, "targets", len(ps.targets)))
        if ps.start_f > ps.end_f: bad.append((p, "in flight"))
        if ps.throttle and ps.throttle.deferred: bad.append((p, "deferred"))
        if ps.is_blocked: bad.append((p, "blocked"))
    if any(rs.is_running for rs in d.state.ripple_states.values()): bad.append(("ripples running",))
    return dict(case=case, align=align, sigma=sigma, ponds=len(s.ponds), bad=bad,
                runs=sum(ps.runs_completed for ps in d.state.pond_states.values()))

if __name__ == "__main__":
    N = int(sys.argv[1])
    with Pool() as pool:
        for r in pool.imap_unordered(run, [(c, a) for c in range(N) for a in (False, True)], chunksize=8):
            print(json.dumps(r), flush=True)
