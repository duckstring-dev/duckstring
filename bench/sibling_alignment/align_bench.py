import json, sys, statistics as st
from multiprocessing import Pool
sys.path.insert(0, "bench/continuous_refresh"); sys.path.insert(0, "tests")
import py_measure as pm
import test_engine as te
from duckstring.engine import NEVER, Trigger, catchment
from duckstring.engine.catchment import is_throttled
C = te._chain_spec
NEW = {
  "twin siblings 3/3":        ([("F", 3, []), ("S", 3, []), ("X", 1, ["F", "S"]), ("O", 1, ["X"])], "O", "F", "S"),
  "near twins 3/3.5":         ([("F", 3, []), ("S", 3.5, []), ("X", 1, ["F", "S"]), ("O", 1, ["X"])], "O", "F", "S"),
  "three siblings 1/3/6":     ([("A", 1, []), ("B", 3, []), ("S", 6, []), ("X", 1, ["A", "B", "S"]), ("O", 1, ["X"])], "O", "A", "S"),
  "fast branch is a chain":   ([("F1", 1, []), ("F2", 1, ["F1"]), ("F3", 1, ["F2"]), ("S", 6, []), ("X", 1, ["F3", "S"]), ("O", 1, ["X"])], "O", "F1", "S"),
  "slow sibling behind lead": ([("I", 1, []), ("P", 1, ["I"]), ("S", 6, ["P"]), ("F", 1, []), ("X", 1, ["S", "F"]), ("O", 1, ["X"])], "O", "F", "S"),
  "nested joins":             ([("A1", 1, []), ("A2", 4, []), ("J1", 1, ["A1", "A2"]), ("B1", 1, []), ("B2", 6, []), ("J2", 1, ["B1", "B2"]), ("J3", 1, ["J1", "J2"]), ("O", 1, ["J3"])], "O", "A1", "B2"),
  "shared upstream twins":    ([("A", 1, []), ("B1", 3, ["A"]), ("B2", 3, ["A"]), ("X", 1, ["B1", "B2"]), ("O", 1, ["X"])], "O", "A", "B1"),
}
te.THROTTLE_TOPOLOGIES.update(NEW); pm.THROTTLE_TOPOLOGIES.update(NEW)
JOINS = {"demo": ["sales"], "slow sibling Inlet": ["X"], "diamond": ["C"], "diamond, long lead on the slow path": ["C"],
         **{k: ["X"] for k in NEW}, "nested joins": ["J1", "J2", "J3"]}

def run(args):
    name, mode, sigma, seed, seconds = args
    catchment._ALIGN = catchment._SOURCE_TURN = catchment._EVERY_FROM_REQUEST = mode != "old"
    _, outlet, inlet, bottleneck = te.THROTTLE_TOPOLOGIES[name]
    s, dur = te._throttle_graph(name)
    s.triggers = {outlet: Trigger(outlet, "wave") if mode == "wave" else Trigger(outlet, "tide", te.secs(0))}
    d = pm.Noisy(s, dur, seed, sigma)
    joins = JOINS.get(name, [])
    seenj = {j: 0 for j in joins}; lead_prev = {j: None for j in joins}
    flips = runsj = 0; gaps = []
    last, ages, half, comps = NEVER, [], {}, []
    for i in range(seconds * 10):
        if i == seconds * 5: half = {p: ps.runs_completed for p, ps in d.state.pond_states.items()}
        d.advance(); d.process()
        stt = d.state.pond_states
        end = stt[outlet].end_f
        if end != last:
            last = end
            if i >= seconds * 5: ages.append((d.now - end).total_seconds()); comps.append(d.now)
        for j in joins:
            if stt[j].runs_started != seenj[j]:
                seenj[j] = stt[j].runs_started
                if i < seconds * 5: continue
                srcs = d.state.ponds[j].sources
                lead = tuple(is_throttled(d.state, sp) for sp in srcs)
                if lead_prev[j] is not None and lead != lead_prev[j]: flips += 1
                lead_prev[j] = lead; runsj += 1
                fs = [stt[sp].end_f for sp in srcs]
                gaps.append((max(fs) - min(fs)).total_seconds())
    runs = {p: ps.runs_completed for p, ps in d.state.pond_states.items()}
    iv = [(b - a).total_seconds() for a, b in zip(comps, comps[1:])]
    ages.sort()
    return dict(topo=name, mode=mode, sigma=sigma, seed=seed, outlet=runs[outlet] - half[outlet],
                bottleneck=runs[bottleneck] - half[bottleneck], total=sum(runs[p] - half[p] for p in runs),
                work_s=d.work_ms / 1000, age=st.mean(ages) if ages else None, age95=ages[int(0.95 * (len(ages) - 1))] if ages else None,
                cv=(st.pstdev(iv) / st.mean(iv)) if len(iv) > 2 else None,
                flip_rate=(flips / runsj) if runsj else None, gap=st.mean(gaps) if gaps else None)

if __name__ == "__main__":
    jobs = [(n, m, sg, sd, 600) for n in te.THROTTLE_TOPOLOGIES for m in ("wave", "old", "new")
            for sg in (0.0, 0.25, 0.5, 1.0) for sd in range(8)]
    with Pool() as p:
        for r in p.imap_unordered(run, jobs, chunksize=4):
            print(json.dumps(r), flush=True)
