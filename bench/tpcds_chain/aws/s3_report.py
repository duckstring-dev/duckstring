"""Summarise an S3 measurement run (aws/jobs/s3_single.sh or s3_cluster.sh) from its downloaded results.

    python aws/s3_report.py results/aws/{run}

For each timed run in runs.tsv, lines up every node's samples.csv (aws/sampler.py, one row a second) with the run's
start and end, and reports network in and out (average and best one-second rate, in Gbit/s, totalled across the
nodes) and CPU busy and waiting on I/O (percent of all cores, averaged across the nodes), with the run's wall time
and each S3 ceiling test (its JSON).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def samples(path: Path) -> list[dict]:
    rows = [{k: float(v) for k, v in r.items()} for r in csv.DictReader(path.open())]
    out = []
    for a, b in zip(rows, rows[1:], strict=False):  # rates over each one-second step
        dt = b["t"] - a["t"]
        out.append({"t": b["t"], "busy": b["cpu_busy"], "iowait": b["cpu_iowait"],
                    "rx": (b["net_rx"] - a["net_rx"]) * 8 / dt / 1e9, "tx": (b["net_tx"] - a["net_tx"]) * 8 / dt / 1e9})
    return out


def main() -> None:
    root = Path(sys.argv[1])
    nodes = {d.name: samples(d / "samples.csv") for d in sorted(root.iterdir()) if (d / "samples.csv").exists()}
    lead = next(d for d in sorted(root.iterdir()) if (d / "runs.tsv").exists())
    spans: dict[str, dict[str, float]] = {}
    for line in (lead / "runs.tsv").read_text().split("\n"):
        if line.strip():
            tag, edge, t = line.split()
            spans.setdefault(tag, {})[edge] = float(t)
    print(f"{len(nodes)} node(s): {', '.join(nodes)}\n")
    print("| run | wall | net in avg / best (Gbit/s) | net out avg / best | CPU busy | CPU waiting on I/O |")
    print("|---|---|---|---|---|---|")
    for tag, span in spans.items():
        a, b = span["start"], span["end"]
        per_second: dict[int, dict[str, float]] = {}
        busy, iowait = [], []
        for rows in nodes.values():
            for r in rows:
                if a <= r["t"] <= b + 0.5:
                    s = per_second.setdefault(int(r["t"]), {"rx": 0.0, "tx": 0.0})
                    s["rx"] += r["rx"]
                    s["tx"] += r["tx"]
                    busy.append(r["busy"])
                    iowait.append(r["iowait"])
        if not per_second:
            continue
        rx = [s["rx"] for s in per_second.values()]
        tx = [s["tx"] for s in per_second.values()]
        print(f"| {tag} | {b - a:.1f} s | {sum(rx) / len(rx):.2f} / {max(rx):.2f} | {sum(tx) / len(tx):.2f} / {max(tx):.2f} "
              f"| {sum(busy) / len(busy):.0f}% | {sum(iowait) / len(iowait):.0f}% |")
    print()
    for d in sorted(root.iterdir()):
        for f in sorted(d.glob("ceiling*.json")):
            c = json.loads(f.read_text())
            print(f"ceiling {d.name}/{f.name}: {c['threads']} threads, avg {c['avg_gbit_s']} Gbit/s, "
                  f"best second {c['best_second_gbit_s']} Gbit/s")
    for f in sorted(lead.glob("duckdb_*_2.json")):
        m = json.loads(f.read_text()).get("metrics", {})
        for stage, v in m.items():
            if v.get("query.total_time"):
                print(f"{f.stem} {stage}: wall {v['query.total_time']:.2f} s, cpu {v['query.cpu_time']:.1f} s, "
                      f"blocked {v['system.blocked_thread_time']:.2f} s, read {(v['io.total_bytes_read'] or 0) / 1e9:.2f} GB, "
                      f"written {(v['io.total_bytes_written'] or 0) / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
