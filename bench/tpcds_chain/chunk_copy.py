"""Copy a snapshot with its fact tables split into 256 MB Parquet files (the dimensions stay single files).

    python chunk_copy.py SRC DST      e.g. s3://…/data/sf1000_mix/snapshot/v1 s3://…/data/sf1000_mixchunked/snapshot/v1

Some engines read one large Parquet file with less parallelism than many smaller ones (StarRocks' FILES(), at
SF100, took 15% longer on the single-file snapshot). The copy gives every engine the same split layout.
Skips a copy whose fact tables are already there.
"""

import sys

import duckdb
import paths
from chain_sql import SOURCES

BIG = ("store_sales", "store_returns")


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    if all(paths.exists(paths.join(dst, t)) for t in BIG):
        print(f"already split at {dst}")
        return
    con = duckdb.connect()
    paths.duckdb_setup(con, src, dst)
    for t in SOURCES:
        target = paths.join(dst, t) if t in BIG else paths.join(dst, f"{t}.parquet")
        opts = "FORMAT parquet, COMPRESSION zstd" + (", FILE_SIZE_BYTES '256MB'" if t in BIG else "")
        con.execute(f"COPY (SELECT * FROM read_parquet('{paths.table(src, t)}')) TO '{target}' ({opts})")
        print(f"  {t} -> {target}", flush=True)


if __name__ == "__main__":
    main()
