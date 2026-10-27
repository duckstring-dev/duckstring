"""Print each Duck's timed calls after a given wall time as an indented tree (depth <= MAXD).

    python report.py LOGDIR SINCE_EPOCH [MAXD]
"""

import re
import sys
from pathlib import Path

logdir, since = Path(sys.argv[1]), float(sys.argv[2])
maxd = int(sys.argv[3]) if len(sys.argv) > 3 else 4
for f in sorted(logdir.glob("*.tsv")):
    lines = f.read_text().splitlines()
    argv = lines[0].split("\t", 1)[1] if lines and lines[0].startswith("#argv") else ""
    m = re.search(r"--name[ =](\S+)", argv) or re.search(r"duck\S* (\S+)", argv)
    rows = []
    for ln in lines[1:]:
        if ln.startswith("#"):
            continue
        t, thread, depth, qual, ms = ln.split("\t")
        t, depth, ms = float(t), int(depth), float(ms)
        if t - ms / 1000 >= since and depth <= maxd:
            rows.append((t - ms / 1000, t, thread, depth, qual, ms))
    if not rows:
        continue
    print(f"\n=== {f.name}: {argv[:160]}")
    # calls are logged on exit; order by start so parents come before children
    for start, _end, thread, depth, qual, ms in sorted(rows, key=lambda r: (r[2], r[0], r[3])):
        print(f"{start - since:7.2f}s {'  ' * depth}{qual:<{48 - 2 * depth}} {ms:8.1f} ms  [{thread}]")
