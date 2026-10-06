"""Clustering benchmark: how row order affects encoding cost, file size, row-group pruning and query time on
TPC-DS ``store_sales``, with DuckDB as the engine. See README.md.

    uv run python bench/clustering/run.py generate --sf 10
    uv run python bench/clustering/run.py run --sf 10
    uv run python bench/clustering/run.py summarise bench/clustering/results/<run>
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import encodings_sql as enc  # noqa: E402

HERE = Path(__file__).parent
ROW_GROUP_ROWS = 122_880  # Duckstring's _ROW_GROUP_ROWS (DuckDB's default), set explicitly on every write
FILE_SIZE = "256MB"
HASH_COLS = ["ss_ticket_number", "ss_item_sk"]  # store_sales' primary key
DIMS = ["date_dim", "item", "customer"]
# Spill cap: a layout that needs more fails (and is recorded as failed) instead of filling the disk.
MAX_TEMP = os.environ.get("BENCH_MAX_TEMP", "60GiB")

# Clustering column sets. "keys" are near-uniform surrogate keys, where scaling and ranking should agree;
# "skewed" pairs the date with a long-tailed amount, where they shouldn't.
SETS = {
    "keys": ["ss_sold_date_sk", "ss_item_sk", "ss_customer_sk"],
    "skewed": ["ss_sold_date_sk", "ss_net_paid"],
}

# Join queries in the style of TPC-DS: a filtered dimension joined to the fact. Pruning on the fact comes
# from DuckDB's dynamic join filters (the build side's min/max pushed into the scan), so a dimension filter
# that selects a contiguous key range prunes and a scattered one doesn't.
JOINS = {
    "join_month": ("ss_sold_date_sk", "date_dim", "d_date_sk", "d_year = 2001 AND d_moy = 11"),
    "join_quarter": ("ss_sold_date_sk", "date_dim", "d_date_sk", "d_year = 2000 AND d_qoy = 2"),
    "join_category": ("ss_item_sk", "item", "i_item_sk", "i_category = 'Music' AND i_class = 'rock'"),
    "join_customer": ("ss_customer_sk", "customer", "c_customer_sk", "c_birth_year = 1970 AND c_birth_month = 3"),
}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sf_dir(data: Path, sf: int) -> Path:
    return data / f"sf{sf}"


def connect(data: Path, threads: int | None, memory: str | None, database: str = ":memory:"):
    con = duckdb.connect(database)
    tmp = data / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{tmp}'")
    con.execute(f"SET max_temp_directory_size = '{MAX_TEMP}'")
    if threads:
        con.execute(f"SET threads = {threads}")
    if memory:
        con.execute(f"SET memory_limit = '{memory}'")
    return con


def timed(con, sql: str) -> tuple[float, list]:
    t = time.perf_counter()
    rows = con.execute(sql).fetchall()
    return time.perf_counter() - t, rows


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*.parquet"))


# ---------------------------------------------------------------------------------------------- generate


def generate(args) -> None:
    root = sf_dir(args.data, args.sf)
    src = root / "source"
    src.mkdir(parents=True, exist_ok=True)
    db = root / "tpcds.duckdb"
    db.unlink(missing_ok=True)
    con = connect(args.data, args.threads, args.memory, str(db))
    con.execute("INSTALL tpcds; LOAD tpcds")
    log(f"dsdgen sf={args.sf} into {db} (single-threaded; roughly 9 s per scale factor)")
    t, _ = timed(con, f"CALL dsdgen(sf = {args.sf})")
    log(f"generated in {t:.0f}s")
    for table in ["store_sales", *DIMS]:
        out = src / table
        shutil.rmtree(out, ignore_errors=True)
        t, _ = timed(con, f"COPY {table} TO '{out}' (FORMAT PARQUET, ROW_GROUP_SIZE {ROW_GROUP_ROWS}, "
                          f"FILE_SIZE_BYTES '{FILE_SIZE}')")
        log(f"exported {table} in {t:.0f}s ({dir_bytes(out) / 1e9:.2f} GB)")
    con.close()
    if not args.keep_db:
        db.unlink()
        Path(f"{db}.wal").unlink(missing_ok=True)
    log("done")


# ---------------------------------------------------------------------------------------------- workload


def _lit(v) -> str:
    return f"'{v}'" if isinstance(v, str) else str(v)


def build_workload(con, source: str, source_dir: Path, cols: list[str], per_family: int, seed: int) -> list[dict]:
    """Query instances for one column set, drawn from a seeded sample so every layout runs the same queries.
    Each has ``family``, ``sql`` (with ``{ss}`` for the fact table) and ``ranges``, the ANDed ``(col, lo, hi)``
    bounds that min/max pruning can use."""
    rng = random.Random(seed)
    sample = {}
    for c in cols:
        vals = con.execute(f"SELECT {c} FROM {source} WHERE {c} IS NOT NULL "
                           f"USING SAMPLE reservoir(200000 ROWS) REPEATABLE ({seed})").fetchall()
        sample[c] = sorted(v[0] for v in vals)

    def window(c: str, sel: float) -> tuple:
        vs = sample[c]
        start = rng.random() * (1 - sel)
        lo = vs[int(start * (len(vs) - 1))]
        hi = vs[min(len(vs) - 1, int((start + sel) * (len(vs) - 1)))]
        return (c, lo, hi)

    def instance(family: str, ranges: list[tuple]) -> dict:
        pred = " AND ".join(f"{c} = {_lit(lo)}" if lo == hi else f"{c} BETWEEN {_lit(lo)} AND {_lit(hi)}"
                            for c, lo, hi in ranges)
        return {"family": family, "ranges": [[c, str(lo), str(hi)] for c, lo, hi in ranges],
                "sql": f"SELECT count(*), sum(ss_net_paid) FROM {{ss}} WHERE {pred}"}

    out = []
    for c in cols:
        for _ in range(per_family):
            v = rng.choice(sample[c])
            out.append(instance(f"point {c}", [(c, v, v)]))
        for sel in (0.001, 0.01, 0.1):
            for _ in range(per_family):
                out.append(instance(f"range {c} {sel:.1%}", [window(c, sel)]))
    for sel in (0.0001, 0.001, 0.01):
        per_dim = sel ** (1 / len(cols))
        for _ in range(per_family):
            out.append(instance(f"box all {sel:.2%}", [window(c, per_dim) for c in cols]))
    for name, (fact_col, dim, key, where) in JOINS.items():
        lo, hi = con.execute(f"SELECT min({key}), max({key}) FROM read_parquet('{source_dir / dim}/*.parquet') "
                             f"WHERE {where}").fetchone()
        out.append({"family": name, "ranges": [[fact_col, str(lo), str(hi)]],
                    "sql": f"SELECT count(*), sum(s.ss_net_paid) FROM {{ss}} s JOIN {dim} d ON s.{fact_col} = d.{key} "
                           f"WHERE {where}"})
    return out


# ---------------------------------------------------------------------------------------------- layouts


def column_stats(con, source: str, cols: list[str]) -> tuple[int, list[dict], float]:
    t = time.perf_counter()
    exprs = ", ".join(f"count(*) FILTER (WHERE {c} IS NULL), min({c}), max({c})" for c in cols)
    row = con.execute(f"SELECT count(*), {exprs} FROM {source}").fetchone()
    stats = [{"nulls": row[1 + 3 * i], "min": row[2 + 3 * i], "max": row[3 + 3 * i]} for i in range(len(cols))]
    return row[0], stats, time.perf_counter() - t


def bits_per_column(rows: int, n: int) -> int:
    """Equal bits per column (Hilbert needs them equal), at least Duckstring's automatic total
    (``dataplane._auto_cluster_bits``: cells just under one row group)."""
    from duckstring.dataplane import _auto_cluster_bits

    return math.ceil(_auto_cluster_bits(rows, n) / n)


def build_layout(con, layout: str, source: str, out: Path, cols: list[str], b: int, stats: list[dict],
                 stats_s: float) -> dict:
    shutil.rmtree(out, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    key_s = 0.0
    if layout.startswith(("qrank_", "arank_")):  # the bucket boundaries are part of this layout's stats pass
        approx = layout.startswith("arank_")
        t = time.perf_counter()
        stats = [dict(s, bounds=con.execute(enc.quantile_bounds_sql(source, c, b, s["nulls"] > 0, approx)).fetchone()[0])
                 for c, s in zip(cols, stats, strict=True)]
        stats_s += time.perf_counter() - t
    if layout in enc.INTERLEAVED:
        key_s, _ = timed(con, f"SELECT count(*), max(_key) FROM ({enc.keyed_sql(layout, source, cols, b, stats)})")
    elif layout == "hash":
        key_s, _ = timed(con, f"SELECT max(hash({', '.join(HASH_COLS)})) FROM {source}")
    sql = enc.ordered_sql(layout, source, cols, b, stats, HASH_COLS)
    write_s, _ = timed(con, f"COPY ({sql}) TO '{out}' (FORMAT PARQUET, ROW_GROUP_SIZE {ROW_GROUP_ROWS}, "
                            f"FILE_SIZE_BYTES '{FILE_SIZE}')")
    return {"layout": layout, "stats_s": stats_s if layout in enc.INTERLEAVED else 0.0, "key_s": key_s, "write_s": write_s,
            "bytes": dir_bytes(out), "files": len(list(out.glob("*.parquet")))}


def row_group_bounds(con, path: Path, cols: list[str]) -> None:
    """Load ``path``'s per-row-group min/max for ``cols`` into the temp table ``rg``."""
    picks = ", ".join(
        f"max(CASE WHEN path_in_schema = '{c}' THEN TRY_CAST(stats_min_value AS DOUBLE) END) AS mn_{c}, "
        f"max(CASE WHEN path_in_schema = '{c}' THEN TRY_CAST(stats_max_value AS DOUBLE) END) AS mx_{c}"
        for c in cols)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE rg AS
        SELECT file_name, row_group_id, any_value(row_group_num_rows) AS n, {picks}
        FROM parquet_metadata('{path}/*.parquet') GROUP BY ALL""")


def pruned_fraction(con, ranges: list[list[str]]) -> float:
    """Share of rows in row groups that min/max statistics can't rule out for ``ranges``."""
    keep = " AND ".join(f"mx_{c} >= {float(lo)!r} AND mn_{c} <= {float(hi)!r}" for c, lo, hi in ranges)
    n_keep, n_all = con.execute(f"SELECT sum(n) FILTER (WHERE {keep}), sum(n) FROM rg").fetchone()
    return (n_keep or 0) / n_all


def time_queries(args, layout_dir: Path, source_dir: Path, workload: list[dict]) -> list[dict]:
    con = connect(args.data, args.threads, args.memory)
    con.execute(f"CREATE VIEW ss AS SELECT * FROM read_parquet('{layout_dir}/*.parquet')")
    for dim in DIMS:
        con.execute(f"CREATE VIEW {dim} AS SELECT * FROM read_parquet('{source_dir / dim}/*.parquet')")
    all_cols = sorted({r[0] for w in workload for r in w["ranges"]})
    row_group_bounds(con, layout_dir, all_cols)
    total = con.execute("SELECT count(*) FROM ss").fetchone()[0]
    results = []
    for i, w in enumerate(workload):
        sql = w["sql"].format(ss="ss")
        for _ in range(args.warmup):
            con.execute(sql).fetchall()
        times, rows = [], None
        for _ in range(args.repeat):
            t, rows = timed(con, sql)
            times.append(t)
        results.append({"query": i, "family": w["family"], "median_ms": statistics.median(times) * 1000,
                        "times_ms": [t * 1000 for t in times], "matched": rows[0][0] / total,
                        "rg_fraction": pruned_fraction(con, w["ranges"])})
    con.close()
    return results


# ---------------------------------------------------------------------------------------------- run


def run(args) -> None:
    root = sf_dir(args.data, args.sf)
    source_dir = root / "source"
    if not (source_dir / "store_sales").exists():
        sys.exit(f"no data at {source_dir}; run `generate --sf {args.sf}` first")
    source = f"read_parquet('{source_dir / 'store_sales'}/*.parquet')"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or HERE / "results" / f"sf{args.sf}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    con = connect(args.data, args.threads, args.memory)
    if (out / "config.json").exists():  # resuming: keep the original config, note the extra layouts
        config = json.loads((out / "config.json").read_text())
        config["layouts"] = list(dict.fromkeys([*config["layouts"], *args.layouts]))
        config["resumed"] = [*config.get("resumed", []), stamp]
        (out / "config.json").write_text(json.dumps(config, indent=2))
    else:
        config = None
    config = config or {"sf": args.sf, "sets": {s: SETS[s] for s in args.sets}, "layouts": args.layouts,
              "repeat": args.repeat, "warmup": args.warmup, "per_family": args.per_family, "seed": args.seed,
              "row_group_rows": ROW_GROUP_ROWS, "file_size": FILE_SIZE, "duckdb": duckdb.__version__,
              "threads": con.execute("SELECT current_setting('threads')").fetchone()[0],
              "memory_limit": con.execute("SELECT current_setting('memory_limit')").fetchone()[0],
              "machine": platform.platform(), "cpu_count": os.cpu_count(), "started": stamp,
              "max_temp_directory_size": MAX_TEMP}
    (out / "config.json").write_text(json.dumps(config, indent=2))
    for set_name in args.sets:
        cols = SETS[set_name]
        rows, stats, stats_s = column_stats(con, source, cols)
        b = args.bits or bits_per_column(rows, len(cols))
        log(f"set {set_name}: {cols}, {rows:,} rows, {b} bits per column")
        wl_path = out / f"{set_name}.workload.json"
        if wl_path.exists():  # resuming into an existing run: keep its queries
            workload = json.loads(wl_path.read_text())
        else:
            workload = build_workload(con, source, source_dir, cols, args.per_family, args.seed)
            wl_path.write_text(json.dumps(workload, indent=2))
        for layout in args.layouts:
            layout_dir = root / set_name / layout
            log(f"  {layout}: building")
            try:
                built = build_layout(con, layout, source, layout_dir, cols, b, stats, stats_s)
            except duckdb.Error as e:
                log(f"  {layout}: FAILED {type(e).__name__}: {str(e).splitlines()[0]}")
                failed = {"set": set_name, "layout": layout, "bits_per_column": b, "rows": rows,
                          "error": f"{type(e).__name__}: {str(e).splitlines()[0]}"}
                with open(out / "encode.jsonl", "a") as f:
                    f.write(json.dumps(failed) + "\n")
                shutil.rmtree(layout_dir, ignore_errors=True)
                continue
            built.update({"set": set_name, "bits_per_column": b, "rows": rows})
            log(f"  {layout}: key {built['key_s']:.1f}s, write {built['write_s']:.1f}s, "
                f"{built['bytes'] / 1e9:.2f} GB; timing {len(workload)} queries")
            queries = time_queries(args, layout_dir, source_dir, workload)
            for r in queries:
                r.update({"set": set_name, "layout": layout})
            with open(out / "encode.jsonl", "a") as f:
                f.write(json.dumps(built) + "\n")
            with open(out / "queries.jsonl", "a") as f:
                for r in queries:
                    f.write(json.dumps(r) + "\n")
            if not args.keep:
                shutil.rmtree(layout_dir)
    summarise(out)
    log(f"results in {out}")


# ---------------------------------------------------------------------------------------------- summary


def summarise(out: Path) -> None:
    """Write ``summary.md`` from a run's ``encode.jsonl`` and ``queries.jsonl``."""
    out = Path(out)
    con = duckdb.connect()
    config = json.loads((out / "config.json").read_text())
    lines = [f"# Clustering benchmark, TPC-DS SF{config['sf']}", "",
             f"DuckDB {config['duckdb']}, {config['threads']} threads, memory limit {config['memory_limit']}, "
             f"{config['machine']}. Queries: median of {config['repeat']} warm runs after {config['warmup']} warm-up.",
             ""]
    encs = [json.loads(line) for line in (out / "encode.jsonl").read_text().splitlines() if line.strip()]
    q_path = f"{out}/queries.jsonl"
    for set_name, cols in config["sets"].items():
        rows = [e for e in encs if e["set"] == set_name]
        if not rows:
            continue
        layouts = [e["layout"] for e in rows]
        lines += [f"## Set `{set_name}`: {', '.join(cols)}", "",
                  f"{rows[0]['rows']:,} rows, {rows[0]['bits_per_column']} bits per column.", "",
                  "| Layout | Stats (s) | Key (s) | Write (s) | Size (GB) |", "|---|---|---|---|---|"]
        for e in rows:
            if e.get("error"):
                lines.append(f"| {e['layout']} | failed: {e['error']} | | | |")
                continue
            lines.append(f"| {e['layout']} | {e['stats_s']:.1f} | {e['key_s']:.1f} | {e['write_s']:.1f} | "
                         f"{e['bytes'] / 1e9:.2f} |")
        layouts = [e["layout"] for e in rows if not e.get("error")]
        for metric, title, fmt in [("rg_fraction", "Share of rows in row groups read (min/max pruning)", "{:.1%}"),
                                   ("median_ms", "Query time, ms (mean over instances of the median)", "{:.0f}")]:
            data = con.execute(f"""SELECT family, layout, avg({metric}), avg(matched) FROM read_json('{q_path}')
                                   WHERE "set" = ? GROUP BY ALL""", [set_name]).fetchall()
            cell = {(f, lay): v for f, lay, v, _ in data}
            matched = {f: m for f, _, _, m in data}
            families = sorted(matched, key=lambda f: (f.split()[0] != "point", f.startswith("join"), f))
            lines += ["", f"### {title}", "", "| Query | Matched | " + " | ".join(layouts) + " |",
                      "|---|---|" + "---|" * len(layouts)]
            for f in families:
                lines.append(f"| {f} | {matched[f]:.3%} | " +
                             " | ".join(fmt.format(cell.get((f, lay), float("nan"))) for lay in layouts) + " |")
        lines.append("")
    (out / "summary.md").write_text("\n".join(lines))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=HERE / "data", help="where generated data and layouts live")
    p.add_argument("--threads", type=int)
    p.add_argument("--memory", help="DuckDB memory_limit, e.g. 24GB")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="generate TPC-DS and export store_sales and dimensions to Parquet")
    g.add_argument("--sf", type=int, required=True)
    g.add_argument("--keep-db", action="store_true", help="keep the intermediate DuckDB database")
    r = sub.add_parser("run", help="build every layout, then measure pruning and query time")
    r.add_argument("--sf", type=int, required=True)
    r.add_argument("--sets", nargs="+", default=list(SETS), choices=list(SETS))
    r.add_argument("--layouts", nargs="+", default=list(enc.LAYOUTS), choices=list(enc.LAYOUTS))
    r.add_argument("--bits", type=int, help="bits per column (default: Duckstring's automatic total, split evenly)")
    r.add_argument("--repeat", type=int, default=5)
    r.add_argument("--warmup", type=int, default=1)
    r.add_argument("--per-family", type=int, default=5, help="query instances per family")
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--keep", action="store_true", help="keep each layout's Parquet (default: delete after measuring)")
    r.add_argument("--out", type=Path)
    s = sub.add_parser("summarise", help="rewrite summary.md for a results directory")
    s.add_argument("out", type=Path)
    args = p.parse_args()
    {"generate": generate, "run": run, "summarise": lambda a: summarise(a.out)}[args.cmd](args)


if __name__ == "__main__":
    main()
