import json, random, statistics as st, sys
from multiprocessing import Pool
sys.path[:0] = ["bench/continuous_refresh", "tests"]
import py_measure as pm
import test_engine as te
from duckstring.engine import Trigger, catchment, pulse_pond

SECONDS = 1200

def make(rng):
    while True:
        n = rng.randint(4, 10); spec = []
        for i in range(n):
            k = rng.choice([0, 1, 1, 2, 2, 3]) if i else 0
            srcs = sorted(rng.sample([f"P{j}" for j in range(i)], min(k, i)))
            secs = [round(rng.uniform(0.3, 5), 1) for _ in range(rng.choice([1, 1, 2]))]
            spec.append((f"P{i}", secs if len(secs) > 1 else secs[0], srcs))
        sinks = {sp for _, _, srcs in spec for sp in srcs}
        outlets = [name for name, _, _ in spec if name not in sinks]
        if len(outlets) >= 2: return spec, outlets

def sim(spec, cad, sigma, seed, clock):
    catchment._ALIGN = True; catchment._EVERY_FROM_REQUEST = clock
    s, dur = te.graph(spec); pulses = {}
    for o, c in cad.items():
        if isinstance(c, tuple): pulses[o] = c[1]
        else: s.triggers[o] = Trigger(o, "tide", te.secs(c))
    d = pm.Noisy(s, dur, seed, sigma)
    for i in range(SECONDS * 10):
        for o, every in pulses.items():
            if i % round(every * 10) == 0: d.state = pulse_pond(d.state, o, d.now)
        d.advance(); d.process()
    out = {}
    for o in cad:
        ts = [t for t in d.state.pond_states[o].completion_times if (t - te.T0).total_seconds() >= SECONDS / 2]
        iv = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
        out[o] = st.mean(iv) if len(iv) >= 3 else None
    return out

def job(args):
    case, clock = args
    rng = random.Random(case); spec, outlets = make(rng)
    cad = {o: rng.choice([0, 2, 5, 5, 10, 10, 20, 30, ("pulse", rng.choice([1, 3]))]) for o in outlets}
    sigma = rng.choice([0, 0.25, 0.5])
    together = sim(spec, cad, sigma, case, clock)
    res = []
    for o, c in cad.items():
        if isinstance(c, tuple) or c == 0: continue
        limit = sim(spec, {o: 0}, sigma, case, clock)[o]       # what its path achieves alone, continuously
        alone = sim(spec, {o: c}, sigma, case, clock)[o]
        res.append(dict(outlet=o, target=c, limit=limit, alone=alone, together=together[o]))
    return dict(case=case, clock=clock, sigma=sigma, outlets=len(outlets), others=[str(c) for c in cad.values()], tides=res)

if __name__ == "__main__":
    with Pool() as p:
        for r in p.imap_unordered(job, [(c, k) for c in range(200) for k in (False, True)]):
            print(json.dumps(r), flush=True)
