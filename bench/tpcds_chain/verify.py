"""Check that two runs of the chain produced the same four tables.

    python verify.py A B

A and B are each a baseline's --out location (DuckDB writes {table}.parquet, Spark a {table}/ directory of
parts), `duckstring:ROOT` for a Catchment's state root, or `duckstring:s3://…` for the data root a Catchment
published to; the last two are read through Duckstring's data plane. Any of them may be local or s3://. Rows are
matched on each table's key (NULL keys match, as GROUP BY groups them) and every other column compared:
exactly, except that numbers may differ by one part in 10^9 (a mean is a double in DuckDB and a DECIMAL in
Spark). A table of more than five million rows (the enriched sales, whose columns are all exact) is compared
by row count and a sum of row hashes instead, which a laptop can do at SF100. Exits non-zero on any difference.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import paths

# Table → (the Pond that publishes it, its key).
TABLES = {
    "enriched": ("sales_enriched", ["ss_ticket_number", "ss_item_sk"]),
    "category_month": ("category_month", ["d_year", "d_moy", "i_category"]),
    "store_quarter": ("store_quarter", ["s_store_name", "s_state", "d_year", "d_qoy"]),
    "customer_value": ("customer_value", ["ss_customer_sk", "ca_state"]),
}
BIG = 5_000_000  # rows above which a table is compared by fingerprint rather than row by row
NUMERIC = ("DECIMAL", "DOUBLE", "FLOAT", "REAL", "HUGEINT", "BIGINT", "INTEGER", "SMALLINT", "TINYINT")


def source_select(spec: str, table: str) -> str:
    if spec.startswith("duckstring:"):
        from duckstring.dataplane import get_data_plane
        from duckstring.storage import get_storage

        loc, pond = spec.removeprefix("duckstring:"), TABLES[table][0]
        if paths.is_remote(loc):  # a data root: {data_root}/{pond}/m1/data
            data_dir = get_storage(loc).child(pond, "m1", "data")
        else:  # a state root: {root}/ponds/{pond}/m1/data
            data_dir = Path(loc) / "ponds" / pond / "m1" / "data"
        return get_data_plane().read_select(data_dir, table)
    return f"SELECT * FROM read_parquet('{paths.table(spec, table)}')"


def compare(con, a: str, b: str, table: str) -> bool:
    key = TABLES[table][1]
    con.execute(f"CREATE OR REPLACE TEMP VIEW a AS {source_select(a, table)}")
    con.execute(f"CREATE OR REPLACE TEMP VIEW b AS {source_select(b, table)}")
    types = {r[0]: r[1].upper() for r in con.sql("DESCRIBE a").fetchall()}
    cols = [c for c in types if c not in key and not c.startswith("_duckstring")]
    match = " AND ".join(f"a.{k} IS NOT DISTINCT FROM b.{k}" for k in key)
    differs = []
    for c in cols:
        if types[c].startswith(NUMERIC):
            differs.append(f"NOT (a.{c} IS NOT DISTINCT FROM b.{c} OR abs(CAST(a.{c} AS DOUBLE) - CAST(b.{c} AS DOUBLE))"
                           f" <= 1e-9 * greatest(1, abs(CAST(b.{c} AS DOUBLE))))")
        else:
            differs.append(f"a.{c} IS DISTINCT FROM b.{c}")
    count_a = con.sql("SELECT count(*) FROM a").fetchall()[0][0]
    count_b = con.sql("SELECT count(*) FROM b").fetchall()[0][0]
    if max(count_a, count_b) > BIG:
        # Too large to join on a laptop: compare an order-independent fingerprint of each side instead, the
        # sum of a hash of every row with numbers rounded as below. Two different multisets would have to
        # collide on a 128-bit sum and on the count to pass.
        norm = ", ".join(f"round(CAST({c} AS DOUBLE), 4)" if types[c].startswith(NUMERIC) else c
                         for c in [*key, *cols])
        fa, fb = (con.sql(f"SELECT sum(hash({norm})::HUGEINT) FROM {v}").fetchall()[0][0] for v in ("a", "b"))
        same = count_a == count_b and fa == fb
        print(f"{table}: {count_a:,} vs {count_b:,} rows; row fingerprints {'match' if fa == fb else 'differ'}: "
              f"{'OK' if same else 'DIFFERENT'}")
        return same
    only = con.sql(f"""
        SELECT count(*) FILTER (WHERE b_present IS NULL), count(*) FILTER (WHERE a_present IS NULL),
               count(*) FILTER (WHERE a_present AND b_present AND ({' OR '.join(differs)}))
        FROM (SELECT a.*, true AS a_present FROM a) a
        FULL JOIN (SELECT b.*, true AS b_present FROM b) b ON {match}""").fetchall()[0]
    same = count_a == count_b and only == (0, 0, 0)
    print(f"{table}: {count_a:,} vs {count_b:,} rows; {only[0]:,} only in A, {only[1]:,} only in B, "
          f"{only[2]:,} with different values: {'OK' if same else 'DIFFERENT'}")
    return same


def main() -> None:
    a, b = sys.argv[1], sys.argv[2]
    import tempfile

    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{tempfile.mkdtemp(prefix='verify_')}'")  # a large table spills
    con.execute("SET preserve_insertion_order = false")
    paths.duckdb_setup(con, a.removeprefix("duckstring:"), b.removeprefix("duckstring:"))
    ok = all([compare(con, a, b, table) for table in TABLES])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
