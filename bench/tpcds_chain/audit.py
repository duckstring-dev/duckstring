"""Audit the work each Pond does: rows scanned per table it read, at two scale factors with the same change.

    python audit.py run --data DIR --root DIR --out FILE.json     one chain, in-process, profiled
    python audit.py compare SMALL.json LARGE.json                  where incremental work grew with the data

`run` builds the chain the way a Duck would, through the same Pond handle and data-plane export, but in one
process and without a Catchment, with DuckDB's profiler on every statement. For each phase (the first build,
then each released batch) it records, per Pond, the rows and row groups each scan read, attributed to the table
behind it (a Source's published files, the landed data, or the Pond's registry), and the Pond's time.

`compare` puts two runs of the same scenario and batch size side by side, at different scale factors. In an
incremental phase a scan should read about the same at either: one that read ten times more at ten times the
data is doing work in proportion to the data, not to the change.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import tomllib

HERE = Path(__file__).resolve().parent
PONDS = ("sales_feed", "dimensions", "sales_enriched", "category_month", "store_quarter", "customer_value")
DDL = re.compile(r"^\s*(CREATE|INSERT|DELETE|UPDATE|COPY|DROP|ALTER)\b", re.IGNORECASE)
COUNT_ONLY = re.compile(r"^\s*SELECT\s+(\(SELECT\s+)?count\(\*\)\s+FROM\s", re.IGNORECASE)


class Profiled:
    """A connection whose statements are profiled: DuckDB writes each one's JSON profile to a file, which is
    read right after a statement that returns no rows, or before the next statement otherwise."""

    def __init__(self, con, sink, path):
        self._con, self._sink, self._path, self._pending = con, sink, path, False
        con.execute("SET enable_profiling = 'json'")
        con.execute(f"SET profiling_output = '{path}'")

    def _collect(self):
        if self._pending and os.path.exists(self._path):
            self._sink(json.loads(Path(self._path).read_text()))
        self._pending = False

    def execute(self, sql, *a, **k):
        self._collect()
        r = self._con.execute(sql, *a, **k)
        self._pending = True
        if DDL.match(str(sql)):
            self._collect()
        return r

    def close(self):
        self._collect()
        self._con.close()

    def __getattr__(self, name):
        return getattr(self._con, name)


def _label(extra: dict) -> str:
    files = str(extra.get("Filename(s)", ""))
    m = re.search(r"ponds/([^/]+)/m\d+/data/([^/,*]+)", files)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.search(r"landing/([^/]+)/([^/,*]+?)\.parquet", files)
    if m:
        return f"landing.{m.group(2)}"
    if "Table" in extra:
        return "own." + str(extra["Table"]).split(".")[-1]
    return str(extra.get("Function", "?")).lower()


def _scans(profile: dict):
    def walk(n):
        if n.get("type") == "TABLE_SCAN":
            yield _label(n.get("extra_info") or {}), n.get("rows_scanned", 0), n.get("row_groups_scanned", 0)
        for c in n.get("children", []):
            yield from walk(c)

    for n in profile.get("operator", []):
        yield from walk(n)


def run(data: Path, root: Path, out: Path) -> None:
    os.environ["TPCDS_BENCH_DATA"] = str(data)
    import duckdb
    from trickle.context import NEVER

    from duckstring.catchment.registry import pond_data_dir, pond_registry_path
    from duckstring.core import Pond, load_ripples
    from duckstring.dataplane import ParquetDataPlane

    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    manifest = json.loads((data / "manifest.json").read_text())
    regs, prev, phases = {}, NEVER, []
    for k in range(0, len(manifest["batches"]) + 1):
        (data / "landing" / "RELEASED").write_text(str(k))
        f = datetime.now(timezone.utc)
        phase = {"phase": "initial" if k == 0 else f"batch_{k}", "ponds": {}}
        for name in PONDS:
            toml = tomllib.loads((HERE / "ponds" / name / "pond.toml").read_text())
            majors = {s: 1 for s in toml.get("sources", {})}
            path = pond_registry_path(root, name, 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            if name not in regs:
                regs[name] = duckdb.connect(str(path))
            reads = defaultdict(lambda: [0, 0])
            big = []  # the statements behind the largest scans

            def sink(profile, reads=reads, big=big):
                if COUNT_ONLY.match(str(profile.get("query", {}).get("sql", ""))):
                    return  # a row count, which DuckDB answers from Parquet metadata though it reports every row
                for label, rows, groups in _scans(profile):
                    reads[label][0] += rows
                    reads[label][1] += groups
                    if rows >= 100_000:
                        sql = " ".join(str(profile.get("query", {}).get("sql", "")).split())
                        big.append({"table": label, "rows": rows, "sql": sql[:400]})

            t = time.perf_counter()
            for ripple in load_ripples(HERE / "ponds" / name).ripples:
                cur = Profiled(regs[name].cursor(), sink, str(root / "profile.json"))
                pond = Pond(name, "1.0.0", cur, root, source_majors=majors,
                            source_f={s: f.isoformat() for s in majors}, f=f, previous_f=prev,
                            own_data_dir=pond_data_dir(root, name, 1, None), staging_dir=path.parent / ".staging")
                ripple["func"](pond)
                cur.close()
            ripples = time.perf_counter() - t
            cur = Profiled(regs[name].cursor(), sink, str(root / "profile.json"))
            ParquetDataPlane().export(cur, pond_data_dir(root, name, 1, None), f=f)
            cur.close()
            phase["ponds"][name] = {"seconds": round(time.perf_counter() - t, 3), "ripple_seconds": round(ripples, 3),
                                    "reads": {lbl: {"rows": r, "row_groups": g} for lbl, (r, g) in sorted(reads.items())},
                                    "big_scans": big}
            print(f"{phase['phase']:8s} {name:16s} {time.perf_counter() - t:6.2f}s  "
                  f"rows scanned {sum(r for r, _ in reads.values()):>12,}", flush=True)
        phases.append(phase)
        prev = f
    out.write_text(json.dumps({"data": str(data), "sf": manifest["sf"], "scenario": manifest.get("scenario"),
                               "batches": manifest["batches"], "phases": phases}, indent=2))


def compare(small_path: Path, large_path: Path) -> None:
    small, large = (json.loads(Path(p).read_text()) for p in (small_path, large_path))
    print(f"scenario {small['scenario']}: SF{small['sf']:g} against SF{large['sf']:g}, "
          f"{small['batches'][0].get('sales_inserted')} and {large['batches'][0].get('sales_inserted')} new sales")
    for ps, pl in zip(small["phases"], large["phases"], strict=True):
        if ps["phase"] == "initial":
            continue
        print(f"\n{ps['phase']}: rows scanned per table (SF{small['sf']:g} -> SF{large['sf']:g}), x = growth")
        for name in PONDS:
            a, b = ps["ponds"][name], pl["ponds"][name]
            print(f"  {name:16s} {a['seconds']:6.2f}s -> {b['seconds']:6.2f}s")
            for lbl in sorted(set(a["reads"]) | set(b["reads"])):
                ra = a["reads"].get(lbl, {}).get("rows", 0)
                rb = b["reads"].get(lbl, {}).get("rows", 0)
                if max(ra, rb) < 1000:
                    continue
                growth = rb / ra if ra else float("inf")
                flag = "  <-- grows with the data" if growth > 3 and rb > 100_000 else ""
                print(f"      {lbl:40s} {ra:>12,} -> {rb:>12,}  x{growth:5.1f}{flag}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data", type=Path, required=True)
    r.add_argument("--root", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare")
    c.add_argument("small", type=Path)
    c.add_argument("large", type=Path)
    args = ap.parse_args()
    if args.cmd == "run":
        run(args.data.resolve(), args.root.resolve(), args.out.resolve())
    else:
        compare(args.small, args.large)


if __name__ == "__main__":
    sys.exit(main())
