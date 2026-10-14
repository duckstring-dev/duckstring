import json, sys, statistics as st
from collections import defaultdict
rows = [json.loads(l) for l in open(sys.argv[1])]
g = defaultdict(list)
for r in rows: g[(r["topo"], r["sigma"], r["mode"])].append(r)
topos = list(dict.fromkeys(r["topo"] for r in rows))
m = lambda L, k: st.mean(x[k] for x in L if x[k] is not None) if any(x[k] is not None for x in L) else float("nan")
print(f"{'topology':34} {'sig':>4} | {'outlet W/old/new':>18} | {'age W/old/new':>17} | {'p95 W/old/new':>17} | {'work new/old':>8} | {'gap old/new':>11} | {'flips old/new':>12} | {'cv W/old/new':>15}")
worse = []
for t in topos:
    for sg in (0.0, 0.25, 0.5, 1.0):
        W, O, N = g[(t, sg, "wave")], g[(t, sg, "old")], g[(t, sg, "new")]
        line = (f"{t[:34]:34} {sg:4.2f} | {m(W,'outlet'):5.1f} {m(O,'outlet'):5.1f} {m(N,'outlet'):5.1f} | "
                f"{m(W,'age'):5.1f} {m(O,'age'):5.1f} {m(N,'age'):5.1f} | {m(W,'age95'):5.1f} {m(O,'age95'):5.1f} {m(N,'age95'):5.1f} | "
                f"{m(N,'work_s')/m(O,'work_s'):8.2f} | {m(O,'gap'):5.1f} {m(N,'gap'):5.1f} | {m(O,'flip_rate'):5.2f} {m(N,'flip_rate'):5.2f} | "
                f"{m(W,'cv'):4.2f} {m(O,'cv'):4.2f} {m(N,'cv'):4.2f}")
        print(line)
        # regressions of new against old
        if m(N, "age") > m(O, "age") * 1.02 or m(N, "outlet") < m(O, "outlet") * 0.98 or m(N, "age95") > m(O, "age95") * 1.05:
            worse.append((t, sg, round(m(O,'outlet'),1), round(m(N,'outlet'),1), round(m(O,'age'),2), round(m(N,'age'),2), round(m(O,'age95'),1), round(m(N,'age95'),1)))
        if m(N, "age") > m(W, "age") * 1.02:
            worse.append(("vs WAVE", t, sg, round(m(W,'age'),2), round(m(N,'age'),2)))
print("\nregressions (new vs old: age +2%, outlet -2%, p95 +5%; or new age > Wave +2%):")
for w in worse: print(" ", w)
