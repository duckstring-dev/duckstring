"""dimensions: the TPC-DS dimensions, landed by gen.py as full extracts.

Each run merges the newest released extract of each table that it hasn't merged yet. merge_table compares
it with the table's state and records only the rows that changed. An extract lands once, so one already
merged is skipped rather than read and compared again. A nightly extract usually matches the last one, so
each is fingerprinted first (its row count and the sum of its row hashes, one read of the file) and merged
only if the fingerprint differs: comparing an unchanged 12M-row customer extract with the table took about
2 s at SF1000. The Pond remembers the extract and fingerprint it last merged per table, in a table outside
the main schema so it isn't published. When nothing changed, the run calls pond.skip(), so downstream Ponds
see the dimensions as unchanged.
"""

import os
import time

import fsspec

from duckstring import ripple

DATA = os.environ["TPCDS_BENCH_DATA"]  # a local path or an s3:// URI


def _loc(*parts: str) -> str:
    return "/".join([DATA.rstrip("/"), *parts])


def _fs():
    return fsspec.core.url_to_fs(DATA)[0]


def _released() -> int:
    """The newest released batch. The benchmark rewrites the marker while runs read it, so it is read in one
    request (opening it on S3 pins its ETag, and a rewrite between the open and the read fails the read)."""
    marker = _loc("landing", "RELEASED")
    for attempt in range(5):
        try:
            return int(_fs().cat_file(marker))
        except FileNotFoundError:
            return 0
        except OSError:  # rewritten mid-read
            if attempt == 4:
                raise
            time.sleep(0.2)
    return 0


def _table(dirpath: str, name: str) -> str:
    """One {name}.parquet file, or a {name}/ directory of chunks (how gen.py writes large tables)."""
    single = f"{dirpath}/{name}.parquet"
    return single if _fs().exists(single) else f"{dirpath}/{name}/*.parquet"


def _s3(con) -> None:
    """Let the registry read landing/ on S3. A Duck with an S3 data root already has a secret; add one only if not."""
    if "://" not in DATA or con.execute("SELECT count(*) FROM duckdb_secrets() WHERE type = 's3'").fetchall()[0][0]:
        return
    con.execute("INSTALL httpfs")  # a fresh machine may not have it yet
    con.execute("LOAD httpfs")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    con.execute(f"CREATE SECRET bench_s3 (TYPE s3, PROVIDER credential_chain, REGION '{region}')")


def _newest(table: str, released: int) -> str:
    for k in range(released, 0, -1):
        batch = _loc("landing", f"batch_{k:03d}")
        if _fs().exists(f"{batch}/{table}.parquet") or _fs().exists(f"{batch}/{table}"):
            return _table(batch, table)
    return _table(_loc("landing", "base"), table)


TABLES = {
    "item": "i_item_sk",
    "store": "s_store_sk",
    "date_dim": "d_date_sk",
    "customer": "c_customer_sk",
    "customer_address": "ca_address_sk",
}


@ripple
def ingest(pond):
    con = pond.con
    _s3(con)
    con.execute("CREATE SCHEMA IF NOT EXISTS bench")
    con.execute("CREATE TABLE IF NOT EXISTS bench.merged (tbl VARCHAR PRIMARY KEY, extract VARCHAR, fp VARCHAR)")
    merged = {t: (e, fp) for t, e, fp in con.execute("SELECT tbl, extract, fp FROM bench.merged").fetchall()}
    changed = False
    released = _released()  # read once, so every table comes from the same release
    for table, pk in TABLES.items():
        extract = _newest(table, released)
        last, last_fp = merged.get(table, (None, None))
        if last == extract:
            continue
        n, h = con.execute(f"SELECT count(*), sum(hash(*COLUMNS(*)))::HUGEINT FROM read_parquet('{extract}')").fetchall()[0]
        fp = f"{n}:{h}"
        if fp != last_fp:
            changed |= pond.merge_table(table, con.sql(f"SELECT * FROM read_parquet('{extract}')"), pk=pk)
        con.execute("INSERT OR REPLACE INTO bench.merged VALUES (?, ?, ?)", [table, extract, fp])
    if not changed:
        pond.skip()
