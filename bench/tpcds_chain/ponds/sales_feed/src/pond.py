"""sales_feed: the store sales fact and its returns, landed by gen.py.

store_sales arrives as a CDC feed with full before and after row images (MySQL's default binlog row image),
so each batch maps directly to a Z-set: a before image is -1, an after image +1. store_returns only ever
grows, so it is an append Trickle.

In the append scenario (gen.py --scenario append, recorded in the data's manifest.json) the feed carries only
new sales, and store_sales is an append Trickle instead.

The first run loads the base snapshot, which is the table's complete state, so it uses merge_table. Each
later run applies the batches released since (the benchmark releases a batch by writing its number to
landing/RELEASED), and remembers the last one it applied in a table outside the main schema, so it isn't
published. A run with no new batch calls pond.skip(), so under a continuous Refresh the Ponds downstream
pass rather than rerun on an empty change.
"""

import json
import os
import time

import fsspec

from duckstring import ripple

DATA = os.environ["TPCDS_BENCH_DATA"]  # a local path or an s3:// URI
SALES_PK = ["ss_ticket_number", "ss_item_sk"]
RETURNS_PK = ["sr_ticket_number", "sr_item_sk"]


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


with fsspec.open(_loc("manifest.json"), "r") as _f:
    APPEND = json.load(_f).get("scenario") == "append"


@ripple
def ingest(pond):
    con = pond.con
    _s3(con)
    con.execute("CREATE SCHEMA IF NOT EXISTS bench")
    con.execute("CREATE TABLE IF NOT EXISTS bench.applied (batch INTEGER)")
    applied = con.execute("SELECT max(batch) FROM bench.applied").fetchall()[0][0]

    first = applied is None
    if first:
        base = _loc("landing", "base")
        sales = con.sql(f"SELECT * FROM read_parquet('{_table(base, 'store_sales')}')")
        if APPEND:
            pond.append_table("store_sales", sales, pk=SALES_PK, fail_on_conflict=False)
        else:
            pond.merge_table("store_sales", sales, pk=SALES_PK)
        pond.append_table(
            "store_returns", con.sql(f"SELECT * FROM read_parquet('{_table(base, 'store_returns')}')"),
            pk=RETURNS_PK, fail_on_conflict=False,
        )
        applied = 0

    released = _released()  # read once: a batch released while this run loads is the next run's
    batches = [_loc("landing", f"batch_{k:03d}") for k in range(applied + 1, released + 1)]
    if batches:
        sales = ", ".join(f"'{_table(b, 'store_sales')}'" for b in batches)
        returns = ", ".join(f"'{_table(b, 'store_returns')}'" for b in batches)
        if APPEND:  # the feed is all new rows
            pond.append_table("store_sales", con.sql(f"SELECT * EXCLUDE (_change) FROM read_parquet([{sales}])"),
                              pk=SALES_PK, fail_on_conflict=False)
        else:
            pond.apply_zset(
                "store_sales",
                con.sql(f"""
                    SELECT * EXCLUDE (_change), CASE _change WHEN 'after' THEN 1 ELSE -1 END AS _duckstring_d
                    FROM read_parquet([{sales}])"""),
                pk=SALES_PK,
            )
        pond.append_table("store_returns", con.sql(f"SELECT * FROM read_parquet([{returns}])"),
                          pk=RETURNS_PK, fail_on_conflict=False)
        applied = released
    elif not first:
        pond.skip()  # nothing released since the last run: downstream Ponds pass instead of rerunning

    con.execute("DELETE FROM bench.applied")
    con.execute("INSERT INTO bench.applied VALUES (?)", [applied])
