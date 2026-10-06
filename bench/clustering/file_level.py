"""File-level clustering (Delta's default: range-partition into files by the curve key, no sort within a file)
against row-group clustering (a global sort), SF100 skewed set, rank-Hilbert key. Measures write time and the
share of rows read when pruning at file vs row-group granularity, with the benchmark's workload.

    uv run python bench/clustering/file_level.py   (after `run.py generate --sf 100` and `run.py run --sf 100`)
"""
import json
import shutil
import sys
import time
from pathlib import Path

import duckdb

BENCH = Path(__file__).parent
sys.path.insert(0, str(BENCH))
import encodings_sql as enc  # noqa: E402

DATA = BENCH / "data"
SRC = f"read_parquet('{DATA}/sf100/source/store_sales/*.parquet')"
COLS = ["ss_sold_date_sk", "ss_net_paid"]
B = 7
OUT = DATA / "filelevel"
OUT.mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
con.execute(f"SET temp_directory = '{DATA}/tmp'; SET max_temp_directory_size = '60GiB'")

stats = []
for c in COLS:
    n, lo, hi = con.execute(f"SELECT count(*) FILTER (WHERE {c} IS NULL), min({c}), max({c}) FROM {SRC}").fetchone()
    s = {"nulls": n, "min": lo, "max": hi}
    s["bounds"] = con.execute(enc.quantile_bounds_sql(SRC, c, B, n > 0, True)).fetchone()[0]
    stats.append(s)
keyed = enc.keyed_sql("arank_hilbert", SRC, COLS, B, stats)
rows = con.execute(f"SELECT count(*) FROM {SRC}").fetchone()[0]
results = {}

for target_bytes, label in [(256 << 20, "256MB"), (1 << 30, "1GB")]:
    # Row-group clustering: global sort, files cut at the target size.
    d_sorted = OUT / f"sorted_{label}"
    shutil.rmtree(d_sorted, ignore_errors=True)
    t = time.perf_counter()
    con.execute(f"COPY (SELECT * EXCLUDE (_key) FROM ({keyed}) ORDER BY _key) TO '{d_sorted}' "
                f"(FORMAT PARQUET, ROW_GROUP_SIZE 122880, FILE_SIZE_BYTES {target_bytes})")
    sorted_s = time.perf_counter() - t
    n_files = len(list(d_sorted.glob("*.parquet")))
    # File-level clustering: the same number of files, each a range of the key, rows unsorted inside.
    d_part = OUT / f"partitioned_{label}"
    shutil.rmtree(d_part, ignore_errors=True)
    t = time.perf_counter()
    cuts = con.execute(f"SELECT approx_quantile(_key, [{', '.join(f'({k}/{n_files})::FLOAT' for k in range(1, n_files))}]) "
                       f"FROM ({keyed})").fetchone()[0]
    file_id = " + ".join(f"(_key > {v})::INT" for v in cuts) or "0"
    con.execute(f"COPY (SELECT * EXCLUDE (_key), {file_id} AS _file FROM ({keyed})) TO '{d_part}' "
                f"(FORMAT PARQUET, ROW_GROUP_SIZE 122880, PARTITION_BY (_file))")
    part_s = time.perf_counter() - t
    results[label] = {"files": n_files, "sorted_write_s": sorted_s, "partitioned_write_s": part_s}
    print(label, results[label], flush=True)

    workload = json.loads((BENCH / "results/sf100/skewed.workload.json").read_text())

    def frac(path: Path, unit: str, ranges) -> float:
        cols = {r[0] for r in ranges}
        picks = ", ".join(
            f"min(CASE WHEN path_in_schema = '{c}' THEN TRY_CAST(stats_min_value AS DOUBLE) END) AS mn_{c}, "
            f"max(CASE WHEN path_in_schema = '{c}' THEN TRY_CAST(stats_max_value AS DOUBLE) END) AS mx_{c}"
            for c in {r[0] for r in ranges})
        group = "file_name" if unit == "file" else "file_name, row_group_id"
        keep = " AND ".join(f"mx_{c} >= {float(lo)!r} AND mn_{c} <= {float(hi)!r}" for c, lo, hi in ranges)
        n_keep, n_all = con.execute(f"""SELECT sum(n) FILTER (WHERE {keep}), sum(n) FROM (
            SELECT {group}, sum(n) AS n, {', '.join(f'min(mn_{c}) AS mn_{c}, max(mx_{c}) AS mx_{c}' for c in cols)}
            FROM (SELECT file_name, row_group_id, any_value(row_group_num_rows) AS n, {picks}
                  FROM parquet_metadata('{path}/**/*.parquet') GROUP BY ALL) GROUP BY {group})""").fetchone()
        return (n_keep or 0) / n_all

    fam = {}
    for w in workload:
        f = fam.setdefault(w["family"], {"sorted_rg": [], "sorted_file": [], "part_rg": []})
        f["sorted_rg"].append(frac(d_sorted, "rg", w["ranges"]))
        f["sorted_file"].append(frac(d_sorted, "file", w["ranges"]))
        f["part_rg"].append(frac(d_part, "rg", w["ranges"]))
    results[label]["families"] = {k: {m: sum(v) / len(v) for m, v in f.items()} for k, f in fam.items()}
    shutil.rmtree(d_sorted)
    shutil.rmtree(d_part)

(BENCH / "results/sf100/filelevel.json").write_text(json.dumps(results, indent=2))
for label, r in results.items():
    print(f"\n{label}: {r['files']} files; write sorted {r['sorted_write_s']:.0f}s, "
          f"partitioned {r['partitioned_write_s']:.0f}s")
    print(f"{'query':32} {'sorted/rg':>10} {'sorted/file':>12} {'partitioned/rg':>15}")
    for k, v in r["families"].items():
        print(f"{k:32} {v['sorted_rg']:10.1%} {v['sorted_file']:12.1%} {v['part_rg']:15.1%}")
