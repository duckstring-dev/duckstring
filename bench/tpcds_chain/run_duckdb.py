"""The plain DuckDB baseline: rebuild the whole chain from a snapshot, as one script.

    python run_duckdb.py --data LOC --version 1 --out LOC [--scratch DIR]

Reads snapshot/v{version}/, writes the enriched sales and the three aggregates as Parquet under --out, and
prints a JSON timing line. The time covers everything from connecting to the last file written. --data and
--out may be local or s3:// (see paths.py); DuckDB spills to --scratch, which should be a local disk (an
instance's NVMe on EC2), and defaults to {out}/_tmp for a local --out. With --profile, DuckDB's own metrics
are recorded per stage (time its threads spent blocked, bytes read and written, CPU time), from its profiler.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

import duckdb
import paths
from chain_sql import AGGREGATES, ENRICHED, SOURCES

METRICS = ["system.blocked_thread_time", "io.total_bytes_read", "io.total_bytes_written", "query.cpu_time",
           "query.total_time", "system.peak_buffer_memory"]


def _metrics(profile_path: str) -> dict:
    p = json.loads(Path(profile_path).read_text())
    flat = {}
    for k, v in p.items():
        if isinstance(v, dict):
            flat.update({f"{k}.{kk}": vv for kk, vv in v.items() if not isinstance(vv, (dict, list))})
        elif not isinstance(v, list):
            flat[k] = v
    return {m: flat.get(m) for m in METRICS}


def run(data: str, version: int, out: str, scratch: str, profile: bool = False) -> dict:
    paths.remove(out)
    paths.makedirs(out)
    snap = paths.join(data, "snapshot", f"v{version}")
    sources = {table: paths.table(snap, table) for table in SOURCES}  # resolved before the clock starts
    t0 = time.perf_counter()
    con = duckdb.connect()
    paths.duckdb_setup(con, data, out)
    con.execute(f"SET temp_directory = '{scratch}'")
    prof = tempfile.mktemp(suffix=".json")
    metrics = {}

    def profiled(name: str, sql: str) -> None:
        if profile:
            con.execute(f"SET enable_profiling = 'json'; SET profiling_output = '{prof}'; "
                        f"SET tracked_metrics = {METRICS}")
        con.execute(sql)
        if profile:
            con.execute("SET enable_profiling = 'no_output'")
            metrics[name] = _metrics(prof)

    for table, target in sources.items():
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{target}')")
    stages = {}
    t = time.perf_counter()
    enriched = paths.join(out, "enriched.parquet")
    profiled("enriched", f"COPY ({ENRICHED}) TO '{enriched}' (FORMAT parquet, COMPRESSION zstd)")
    stages["enriched"] = time.perf_counter() - t
    con.execute(f"CREATE VIEW enriched AS SELECT * FROM read_parquet('{enriched}')")
    for name, sql in AGGREGATES.items():
        t = time.perf_counter()
        profiled(name, f"COPY ({sql}) TO '{paths.join(out, f'{name}.parquet')}' (FORMAT parquet, COMPRESSION zstd)")
        stages[name] = time.perf_counter() - t
    con.close()
    total = time.perf_counter() - t0
    shutil.rmtree(scratch, ignore_errors=True)
    return {"engine": "duckdb", "duckdb": duckdb.__version__, "version": version,
            "seconds": round(total, 2), "stages": {k: round(v, 2) for k, v in stages.items()},
            **({"metrics": metrics} if profile else {})}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--version", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--scratch", help="a local directory for DuckDB to spill to")
    ap.add_argument("--profile", action="store_true", help="record DuckDB's own I/O and CPU metrics per stage")
    args = ap.parse_args()
    out = args.out if paths.is_remote(args.out) else str(Path(args.out).resolve())
    scratch = args.scratch or (tempfile.mkdtemp(prefix="duckdb_spill_") if paths.is_remote(out)
                               else paths.join(out, "_tmp"))
    print(json.dumps(run(args.data, args.version, out, scratch, args.profile)))


if __name__ == "__main__":
    main()
