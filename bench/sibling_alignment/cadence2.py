"""Several Outlets with distinct cadences over shared upstream: each Outlet's mean completion interval (second
half) against its target and against the same Outlet with only its own trigger."""
import json, math, statistics as st, sys
from multiprocessing import Pool
sys.path[:0] = ["bench/continuous_refresh", "tests"]
import py_measure as pm
import test_engine as te
from duckstring.engine import Trigger, catchment, pulse_pond

# name -> (spec, {outlet: cadence}) ; cadence: seconds for a Tide, 0 for continuous, ("pulse", s) for Pulses
SCEN = {
  "shared chain, 3 Outlets":  ([("I", 1, []), ("S", 3, ["I"]), ("O1", 1, ["S"]), ("O2", 1, ["S"]), ("O3", 1, ["S"])],
                               {"O1": 0, "O2": 10, "O3": 30}),
  "shared Inlet, two bottlenecks": ([("I", 1, []), ("B1", 5, ["I"]), ("O1", 1, ["B1"]), ("B2", 2, ["I"]), ("O2", 1, ["B2"])],
                               {"O1": 7, "O2": 0}),
  "demo, report + live":      ([("transactions", 1, []), ("products", 2, []), ("sales", [2, 3], ["transactions", "products"]),
                                ("reports", 1, ["sales"]), ("live", 1, ["sales"])], {"reports": 10, "live": 0}),
  "both Tides below the bottleneck": ([("I", 1, []), ("S", 4, ["I"]), ("O1", 1, ["S"]), ("O2", 1, ["S"])], {"O1": 1, "O2": 2}),
  "Pulses next to a Tide":    ([("I", 1, []), ("S", 3, ["I"]), ("O1", 1, ["S"]), ("O2", 1, ["S"])], {"O1": ("pulse", 2), "O2": 15}),
  "join shared by two Outlets": ([("F", 1, []), ("S", 6, []), ("X", 1, ["F", "S"]), ("O1", 1, ["X"]), ("O2", 1, ["X", "F"])],
                               {"O1": 20, "O2": 0}),
  "three cadences, long lead": ([("I", 1, []), ("P1", 1, ["I"]), ("P2", 1, ["P1"]), ("P3", 1, ["P2"]), ("B", 3, ["P3"]),
                                 ("O1", 1, ["B"]), ("O2", 1, ["B"]), ("O3", 1, ["P2"])], {"O1": 0, "O2": 12, "O3": 5}),
}

def simulate(spec, cadences, sigma, seed, seconds, align, clock=True):
    catchment._ALIGN = align; catchment._EVERY_FROM_REQUEST = clock
    s, dur = te.graph(spec)
    if spec[2][0] == "sales":   # the demo's sales: daily 2 s and tiers 1 s feed join 3 s
        from duckstring.engine import Ripple, RippleState
        s.ripples["sales.r1"] = Ripple("sales.r1", "sales", "tiers", parents=[]); dur["sales.r1"] = te.secs(1)
        s.ripples["sales.r2"] = Ripple("sales.r2", "sales", "join", parents=["sales.r0", "sales.r1"]); dur["sales.r2"] = te.secs(3)
        s.ripple_states["sales.r2"] = RippleState()
    pulses = {}
    for o, c in cadences.items():
        if isinstance(c, tuple): pulses[o] = c[1]
        else: s.triggers[o] = Trigger(o, "tide", te.secs(c))
    d = pm.Noisy(s, dur, seed, sigma)
    for i in range(seconds * 10):
        for o, every in pulses.items():
            if i % round(every * 10) == 0: d.state = pulse_pond(d.state, o, d.now)
        d.advance(); d.process()
    out = {}
    for o in cadences:
        times = [t for t in d.state.pond_states[o].completion_times if (t - te.T0).total_seconds() >= seconds / 2]
        iv = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
        out[o] = st.mean(iv) if iv else None
    return out

def job(args):
    name, sigma, seed, clock, alone = args; align = True
    spec, cad = SCEN[name]
    if alone:
        res = {}
        for o, c in cad.items():
            res.update(simulate(spec, {o: c}, sigma, seed, 1800, align, clock))
    else:
        res = simulate(spec, cad, sigma, seed, 1800, align, clock)
    return dict(name=name, sigma=sigma, seed=seed, clock=clock, alone=alone, iv=res)

if __name__ == "__main__":
    jobs = [(n, sg, sd, al, alone) for n in SCEN for sg in (0.0, 0.5) for sd in range(6) for al in (False, True) for alone in (False, True)]
    with Pool() as p:
        for r in p.imap_unordered(job, jobs): print(json.dumps(r), flush=True)
