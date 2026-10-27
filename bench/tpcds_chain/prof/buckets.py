"""Split each Pond's change Run into buckets, by exclusive time of the counted calls.

    python buckets.py RESULTS_JSON LOGDIR
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

BUCKETS = {  # function -> bucket; a bucket's time excludes counted calls nested inside it
    "Pond.read_table": "read Source state",
    "Pond.read_delta": "read Source change",
    "TrickleBuilder._over_threshold": "count Source",
    "TrickleBuilder._scratch_table": "join (inner deltas)",
    "io.apply_aggregate": "aggregate",
    "io.apply_zset": "join + write change",
    "io.append_table": "write",
    "io.merge_table": "write",
    "TrickleBuilder._chain": "chain prep",
    "executor._export_data": "publish",
    "executor._run_ripple": "Ripple, other",
}
ORDER = ["start", "read Source state", "read Source change", "count Source", "join (inner deltas)",
         "join + write change", "aggregate", "write", "chain prep", "Ripple, other", "publish", "end"]

res, logdir = json.load(open(sys.argv[1])), Path(sys.argv[2])
ph = res["phases"][1]
P = lambda s: datetime.fromisoformat(s).timestamp()  # noqa: E731
t0 = min(P(r["started_at"]) for r in ph["runs"])
calls = {}
for f in logdir.glob("*.tsv"):
    lines = f.read_text().splitlines()
    m = re.search(r"--pond (\S+)", lines[0])
    if not m:
        continue
    rows = []
    for ln in lines[1:]:
        t, thread, depth, qual, ms = ln.split("\t")
        if qual in BUCKETS:
            end = float(t)
            rows.append((end - float(ms) / 1000, end, thread, qual))
    calls.setdefault(m.group(1), []).extend(rows)  # a respawned Duck logs to a second file

print(f"| Pond | {' | '.join(ORDER)} |\n|---|{'---|' * len(ORDER)}")
for r in sorted(ph["runs"], key=lambda r: r["started_at"]):
    pond, a, b = r["pond"], P(r["started_at"]), P(r["finished_at"])
    rip = r["ripples"][0]
    sel = [c for c in calls.get(pond, []) if c[0] >= a - 0.05 and c[1] <= b + 0.5]
    tot = {k: 0.0 for k in ORDER}
    for c in sel:
        inner = [d for d in sel if d is not c and d[2] == c[2] and d[0] >= c[0] and d[1] <= c[1]]
        # only direct counted children: drop those nested in another inner one
        direct = [d for d in inner if not any(e is not d and e[0] <= d[0] and e[1] >= d[1] for e in inner)]
        tot[BUCKETS[c[3]]] += (c[1] - c[0]) - sum(d[1] - d[0] for d in direct)
    tot["start"] = a - t0
    tot["end"] = b - t0
    print(f"| {pond} | " + " | ".join(f"{tot[k]:.2f}" if tot[k] else "" for k in ORDER) + " |")
