"""Summarise results/*.jsonl: continuous / Wave ratios per topology and noise level, and engine agreement.

    python bench/continuous_refresh/analyse.py py    # or ts
"""

import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
engine = sys.argv[1] if len(sys.argv) > 1 else "py"
rows = [json.loads(line) for f in ("py", "ts") if (HERE / "results" / f"{f}.jsonl").exists()
        for line in open(HERE / "results" / f"{f}.jsonl")]
pairs: dict = defaultdict(dict)
for r in rows:
    pairs[(r["topo"], r["sigma"], r["seed"], r["mode"])][r["engine"]] = r
keys = ("outlet", "bottleneck", "inlet", "total")
both = [v for v in pairs.values() if "py" in v and "ts" in v]
print(f"engines agree on every run count in {sum(all(v['py'][k] == v['ts'][k] for k in keys) for v in both)}/{len(both)} runs")

seeds = sorted({r["seed"] for r in rows})
print(f"\n[{engine}] second half of each run, mean over {len(seeds)} seeds; ratios are continuous / Wave")
print(f"{'topology':40} {'sigma':>5} {'outlet W':>8} {'C/W':>5} {'inlet C/W':>9} {'work C/W':>8} "
      f"{'age W':>6} {'age C':>6} {'p95 W':>6} {'p95 C':>6}")
summary = defaultdict(list)
for topo in dict.fromkeys(r["topo"] for r in rows):
    for sigma in (0.0, 0.25, 0.5, 1.0):
        W = [pairs[(topo, sigma, s, "wave")].get(engine) for s in seeds]
        C = [pairs[(topo, sigma, s, "cont")].get(engine) for s in seeds]
        W, C = [x for x in W if x], [x for x in C if x]
        if not W or not C:
            continue

        def m(rs, k):
            return st.mean(x[k] for x in rs)

        ro, ri, rw = m(C, "outlet") / m(W, "outlet"), m(C, "inlet") / m(W, "inlet"), m(C, "work_s") / m(W, "work_s")
        summary[sigma].append((ro, rw, m(C, "age") / m(W, "age")))
        print(f"{topo[:40]:40} {sigma:5.2f} {m(W, 'outlet'):8.1f} {ro:5.2f} {ri:9.2f} {rw:8.2f} "
              f"{m(W, 'age'):6.1f} {m(C, 'age'):6.1f} {m(W, 'age95'):6.1f} {m(C, 'age95'):6.1f}")
print()
for sigma, v in sorted(summary.items()):
    print(f"sigma {sigma}: Outlet runs C/W min {min(x[0] for x in v):.2f}, median {st.median(x[0] for x in v):.2f}; "
          f"work C/W median {st.median(x[1] for x in v):.2f}, max {max(x[1] for x in v):.2f}; "
          f"age C/W median {st.median(x[2] for x in v):.2f}, max {max(x[2] for x in v):.2f}")
