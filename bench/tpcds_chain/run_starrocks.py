"""The StarRocks baseline: rebuild the whole chain from a snapshot on S3, as one job against a running StarRocks.

    python run_starrocks.py --data s3://… --version 1 --out s3://… [--host 127.0.0.1] [--port 9030]

Runs the queries in chain_sql.py, reading the snapshot's Parquet straight from S3 with FILES() and writing the
enriched sales and the three aggregates back to S3 with INSERT INTO FILES, then reading the enriched sales
back for the aggregates, as the DuckDB and Spark baselines do. Nothing is loaded into StarRocks' own tables.

Each FILES() read declares its columns (the "schema" property, StarRocks 4.1.2+), as a pipeline whose tables are
known would, instead of having StarRocks infer them on every query by sampling files and unionising every
file's schema. The sources' schemas are read from their Parquet footers before the clock starts; the enriched
table's, from one of the files StarRocks wrote, inside the timed run. --infer leaves inference on.
Prints a JSON timing line; the time covers everything from connecting to the last file written, not starting
the StarRocks cluster (as for the Catchment, and unlike Spark, whose session starts with each job).

--container-data DIR runs on local files instead (the laptop): the snapshot's tables are found under --data on
this machine and read by StarRocks from DIR, a copy inside its container, with file:// paths; --out is then a path
inside the container, and the outputs stay there (copy them out to check them).

S3 access: the backends' instance profile, or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (and
AWS_SESSION_TOKEN) from the environment, passed to StarRocks as FILES() properties (for a laptop container).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import duckdb
import paths
import pymysql
from chain_sql import AGGREGATES, ENRICHED, SOURCES

KEYWORDS = {"ON", "WHERE", "GROUP", "JOIN", "LEFT", "RIGHT", "INNER", "FULL", "ORDER", "LIMIT", "UNION"}


def _lit(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def s3_properties() -> list[str]:
    key, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
    if key and secret:
        props = [f'"aws.s3.access_key" = {_lit(key)}', f'"aws.s3.secret_key" = {_lit(secret)}']
        if os.environ.get("AWS_SESSION_TOKEN"):
            props.append(f'"aws.s3.session_token" = {_lit(os.environ["AWS_SESSION_TOKEN"])}')
    else:
        props = ['"aws.s3.use_instance_profile" = "true"']
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return props + [f'"aws.s3.region" = {_lit(region)}']


def schema_of(path: str) -> str:
    """A Parquet table's columns as StarRocks types, read from a file's footer with DuckDB."""
    con = duckdb.connect()
    paths.duckdb_setup(con, path)
    cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    kinds = {"INTEGER": "INT", "VARCHAR": "STRING", "TIMESTAMP": "DATETIME", "HUGEINT": "LARGEINT"}
    return ", ".join(f"{name} {kinds.get(kind, kind)}" for name, kind, *_ in cols)


def files(path: str, schema: str | None = None) -> str:
    """A table over Parquet files. A subquery, because StarRocks can't qualify columns by a FILES() alias."""
    props = [f'"path" = {_lit(path)}', '"format" = "parquet"', *(s3_properties() if "s3://" in path else [])]
    if schema:
        props.append(f'"schema" = {_lit(schema)}')
    return f"(SELECT * FROM FILES({', '.join(props)}))"


def bind(sql: str, tables: dict[str, str], schemas: dict[str, str | None]) -> str:
    """Replace each table name after FROM or JOIN with its FILES() subquery, keeping its alias (or using the
    table's name as the alias when it has none)."""
    def repl(m: re.Match) -> str:
        kw, name, alias = m.group(1), m.group(2), m.group(3)
        table = files(tables[name], schemas.get(name))
        if alias and alias.upper() not in KEYWORDS:
            return f"{kw} {table} {alias}"
        return f"{kw} {table} {name}" + (f" {alias}" if alias else "")
    names = "|".join(re.escape(t) for t in tables)
    return re.sub(rf"\b(FROM|JOIN)\s+({names})\b(?:\s+(\w+))?", repl, sql)


def run(data: str, version: int, out: str, host: str, port: int, infer: bool = False,
        container_data: str | None = None) -> dict:
    if not container_data:
        paths.remove(out)
    snap = paths.join(data, "snapshot", f"v{version}")
    sources = {table: paths.table(snap, table) for table in SOURCES}  # resolved before the clock starts
    schemas = {table: None if infer else schema_of(path) for table, path in sources.items()}
    if container_data:  # the same files, as StarRocks sees them inside its container
        sources = {t: "file://" + p.replace(str(data).rstrip("/"), container_data.rstrip("/"), 1)
                   for t, p in sources.items()}
    t0 = time.perf_counter()
    con = pymysql.connect(host=host, port=port, user="root", autocommit=True, connect_timeout=30, read_timeout=7200)
    cur = con.cursor()
    cur.execute("SET query_timeout = 7200")
    cur.execute("SET enable_spill = true")
    cur.execute("SELECT current_version()")
    version_str = cur.fetchall()[0][0]

    def write(name: str, sql: str) -> float:
        props = [f'"path" = {_lit(paths.join(out, name) + "/")}', '"format" = "parquet"', '"compression" = "zstd"',
                 *(s3_properties() if "s3://" in out else [])]
        t = time.perf_counter()
        cur.execute(f"INSERT INTO FILES({', '.join(props)}) {sql}")
        return time.perf_counter() - t

    stages = {"enriched": write("enriched", bind(ENRICHED, sources, schemas))}
    enriched = {"enriched": paths.join(out, "enriched", "*.parquet")}
    t = time.perf_counter()
    if infer or container_data:  # the container's outputs aren't visible here to read a schema from
        enriched_schema = {"enriched": None}
    else:
        first = paths.fs(out).glob(enriched["enriched"])[0]  # s3fs lists without the scheme
        enriched_schema = {"enriched": schema_of(first if "://" in first or not paths.is_remote(out) else "s3://" + first)}
    stages["enriched_schema"] = time.perf_counter() - t
    for name, sql in AGGREGATES.items():
        stages[name] = write(name, bind(sql, enriched, enriched_schema))
    con.close()
    total = time.perf_counter() - t0
    return {"engine": "starrocks", "starrocks": version_str, "version": version, "schema": "inferred" if infer else "declared",
            "seconds": round(total, 2),
            "stages": {k: round(v, 2) for k, v in stages.items()}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--version", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9030)
    ap.add_argument("--infer", action="store_true", help="let FILES() infer each table's schema")
    ap.add_argument("--container-data", help="read local files: --data's copy inside StarRocks' container")
    args = ap.parse_args()
    data = args.data if paths.is_remote(args.data) or not args.container_data else str(Path(args.data).resolve())
    print(json.dumps(run(data, args.version, args.out, args.host, args.port, args.infer, args.container_data)))


if __name__ == "__main__":
    main()
