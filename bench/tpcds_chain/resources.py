"""Run a benchmark command and record the memory and disk it uses: what to size a cloud machine by.

    python resources.py --out FILE.json [--spill GLOB ...] [--interval 0.25] -- COMMAND ...

Samples the command's whole process tree (the Catchment and its Ducks for Duckstring, the JVM for Spark)
and the machine, and writes a timeline and the peaks:

- rss: resident memory per process, labelled by role (a Duck by its Pond's name), and the tree's total.
- swap: the machine's swap in use above where it started. On a Mac, memory pressure shows up here and in
  compressed memory before anything fails, so a run can "fit" while paging.
- disk_used: the volume's used bytes above where they started. Its peak minus its end is the scratch space a
  run needed and gave back (DuckDB's spill, Spark's shuffle files); the end is what it left behind.
- spill: the bytes under each --spill glob (DuckDB's temp directories), sampled, for attribution.
- disk_written: bytes written to disk by the machine during the run (all processes).
- cpu: CPU per process role, in cores (a Duck by its Pond's name), and the machine's memory in use, page cache
  and available, so a run's RAM can be told apart from the cache its reads fill.

Each sample's t is seconds from the start; t0_epoch (wall clock) lines the timeline up with the Catchment's run
history and the Ducks' own memory samples (prof/sitecustomize.py, DS_MEM_LOG).

Lines the command prints are kept with their time, so a phase (a runner's JSON line) can be lined up with
the timeline. Needs psutil (`uv run --with psutil python resources.py ...`), which the command doesn't.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

PONDS = ("sales_feed", "dimensions", "sales_enriched", "category_month", "store_quarter", "customer_value")


def label(p: psutil.Process) -> str:
    try:
        cmd = " ".join(p.cmdline())
    except (psutil.Error, OSError):
        return "?"
    if "duckstring.duck" in cmd:
        for name in PONDS:
            if re.search(rf"\b{name}\b", cmd):
                return f"duck:{name}"
        return "duck:?"
    if "java" in os.path.basename(p.info["name"] if hasattr(p, "info") else p.name()).lower() or "spark" in cmd:
        return "spark"
    if "catchment" in cmd or "uvicorn" in cmd:
        return "catchment"
    return Path(cmd.split(" ")[1] if " " in cmd else cmd).name or "?"


def tree_size(paths: list[str]) -> int:
    total = 0
    for root in paths:
        for dirpath, _, files in os.walk(root):
            for f in files:
                try:
                    total += os.lstat(os.path.join(dirpath, f)).st_size
                except OSError:
                    pass
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--spill", action="append", default=[], help="a glob of spill directories to size")
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd

    disk0 = shutil.disk_usage("/").used
    swap0 = psutil.swap_memory().used
    io0 = psutil.disk_io_counters().write_bytes
    t0, t0_epoch = time.perf_counter(), time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1)
    lines: list[dict] = []

    def pump():
        for line in proc.stdout:
            sys.stdout.write(line)
            lines.append({"t": round(time.perf_counter() - t0, 2), "line": line.rstrip()})

    threading.Thread(target=pump, daemon=True).start()
    root = psutil.Process(proc.pid)
    timeline, peak_rss, peak = [], {}, {"rss_total": 0, "swap": 0, "disk_used": 0, "spill": {}}
    cpu_seen: dict[int, psutil.Process] = {}  # cpu_percent needs the same Process object between samples
    while proc.poll() is None:
        t = round(time.perf_counter() - t0, 2)
        rss: dict[str, int] = {}
        try:
            procs = [root, *root.children(recursive=True)]
        except psutil.Error:
            procs = []
        cpu: dict[str, float] = {}
        for p in procs:
            p = cpu_seen.setdefault(p.pid, p)
            try:
                r = p.memory_info().rss
                c = p.cpu_percent(None) / 100
            except psutil.Error:
                continue
            k = label(p)
            rss[k] = rss.get(k, 0) + r
            cpu[k] = round(cpu.get(k, 0) + c, 2)
        total = sum(rss.values())
        swap = psutil.swap_memory().used - swap0
        disk = shutil.disk_usage("/").used - disk0
        spill = {g: tree_size(glob.glob(g)) for g in args.spill}
        for k, v in rss.items():
            peak_rss[k] = max(peak_rss.get(k, 0), v)
        peak["rss_total"] = max(peak["rss_total"], total)
        peak["swap"] = max(peak["swap"], swap)
        peak["disk_used"] = max(peak["disk_used"], disk)
        for g, v in spill.items():
            peak["spill"][g] = max(peak["spill"].get(g, 0), v)
        vm = psutil.virtual_memory()
        timeline.append({"t": t, "rss_total": total, "rss": rss, "cpu": cpu, "swap": swap, "disk_used": disk,
                         "available": vm.available, "used": vm.used,
                         "cached": getattr(vm, "cached", 0) + getattr(vm, "buffers", 0), "spill": spill})
        time.sleep(args.interval)

    end_disk = shutil.disk_usage("/").used - disk0
    result = {
        "cmd": cmd, "exit": proc.returncode, "seconds": round(time.perf_counter() - t0, 2), "t0_epoch": t0_epoch,
        "interval": args.interval,
        "ram_total": psutil.virtual_memory().total,
        "peak": {**peak, "rss": dict(sorted(peak_rss.items(), key=lambda kv: -kv[1]))},
        "disk_used_end": end_disk, "scratch": peak["disk_used"] - end_disk,
        "disk_written": psutil.disk_io_counters().write_bytes - io0,
        "lines": lines, "timeline": timeline,
    }
    args.out.write_text(json.dumps(result))
    gb = 1e9
    print(f"[resources] {result['seconds']}s, exit {proc.returncode}: peak tree RSS "
          f"{peak['rss_total'] / gb:.1f} GB, swap +{peak['swap'] / gb:.1f} GB, disk peak +{peak['disk_used'] / gb:.1f} GB "
          f"(end +{end_disk / gb:.1f}, scratch {result['scratch'] / gb:.1f}), written {result['disk_written'] / gb:.1f} GB",
          file=sys.stderr)
    for k, v in result["peak"]["rss"].items():
        print(f"[resources]   {k:24s} {v / gb:6.2f} GB", file=sys.stderr)
    for g, v in peak["spill"].items():
        print(f"[resources]   spill {g}: {v / gb:.2f} GB", file=sys.stderr)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
