"""Generate the benchmark's data: a TPC-DS base snapshot, and change batches against it.

    python gen.py --sf 100 --data /path/to/data [--batches 1] [--scenario mix] [--change 1.0 | --size N]
                  [--base-from /path/to/other/data | --source s3://redshift-downloads/TPC-DS/2.13/100GB]

Writes, under --data:

    landing/base/{table}.parquet           the base snapshot (the columns the chain uses)
    landing/batch_{k}/store_sales.parquet  a CDC feed: full before and after row images (`_change`)
    landing/batch_{k}/store_returns.parquet  new returns (append-only)
    landing/batch_{k}/{item,customer}.parquet  full dimension extracts, after this batch's changes
    snapshot/v{k}/{table}.parquet          the full state after batch k (what the baselines read)
    manifest.json                          row counts and the change in each batch

Duckstring's Inlets read landing/, as a pipeline fed by CDC and nightly extracts would. The baselines rebuild
from snapshot/v{k}/, as a batch job over a fresh copy of the sources would. Nothing here is timed.

Every change is chosen by hashing keys with the batch number, so a run is reproducible. --change scales every
fraction at once (for the run-time against change-fraction curve); --size sets it so a batch has about N new
sales, the same at any scale factor (for checking that incremental work follows the change, not the data).

--scenario picks what a batch changes:

    append   new sales only (0.5% of the fact), loaded as an append Trickle; nothing else changes
    fact     new, corrected and voided sales and new returns; the dimensions don't change
    mix      fact plus reclassified items and moved customers (the default)

--base-from hard-links another data directory's base, so scenarios share one generated base.

--source builds the base from pre-generated TPC-DS text instead of running dsdgen: AWS publishes it in
us-east-1 at s3://redshift-downloads/TPC-DS/2.13/{10GB,100GB,1TB,3TB,10TB,30TB,100TB}/ (pipe-delimited,
gzipped, one directory per table). It's read with the column names and types of DuckDB's own TPC-DS schema,
so both routes give the same columns; at 1 TB, generating with dsdgen would need about 3.3 TB of scratch.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

import duckdb

# The columns the chain reads. Everything else in TPC-DS is dropped at generation.
COLUMNS = {
    "store_sales": "ss_ticket_number, ss_item_sk, ss_sold_date_sk, ss_store_sk, ss_customer_sk, ss_quantity, "
                   "ss_sales_price, ss_ext_sales_price, ss_net_paid, ss_net_profit",
    "store_returns": "sr_ticket_number, sr_item_sk, sr_returned_date_sk, sr_return_quantity, sr_return_amt, "
                     "sr_net_loss",
    "item": "i_item_sk, i_item_id, i_brand, i_class, i_category, i_current_price",
    "store": "s_store_sk, s_store_name, s_state, s_market_id",
    "date_dim": "d_date_sk, d_date, d_year, d_moy, d_qoy",
    "customer": "c_customer_sk, c_customer_id, c_current_addr_sk, c_birth_year, c_preferred_cust_flag",
    "customer_address": "ca_address_sk, ca_state, ca_city",
}

# Fractions of rows changed per batch, before --change scales them. Together they touch about 0.5% of the
# enriched sales: 0.2% new sales, 0.05% corrected, 0.02% voided, 0.03% returned, the sales of the 0.05% of
# items reclassified, and the sales of the 0.1% of customers who moved.
FRACTIONS = {
    "sales_insert": 0.002,
    "sales_update": 0.0005,
    "sales_delete": 0.0002,
    "returns_insert": 0.0003,
    "item_update": 0.0005,
    "customer_update": 0.001,
}

# What each scenario changes, as multipliers on FRACTIONS.
SCENARIOS = {
    "append": {"sales_insert": 2.5, "sales_update": 0, "sales_delete": 0, "returns_insert": 0, "item_update": 0,
               "customer_update": 0},
    "fact": {"item_update": 0, "customer_update": 0},
    "mix": {},
}

# Parts per million for the hash buckets.
PPM = 1_000_000


def _copy(con, sql: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)")


def _count(con, path: Path) -> int:
    return con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchall()[0][0]


def _store_sales_filter() -> str:
    # The fact keeps only rows every join can place, as TPC-DS queries do with inner joins on date and
    # store. Customer stays nullable (a left join), as do returns.
    return " WHERE ss_sold_date_sk IS NOT NULL AND ss_store_sk IS NOT NULL"


def convert_base(con, source: str, data: Path) -> None:
    """The base from pre-generated TPC-DS text (see --source), keeping the columns the chain reads."""
    base = data / "landing" / "base"
    schema = duckdb.connect()
    schema.execute("INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf=0)")  # the empty tables: names and types
    con.execute("INSTALL httpfs; LOAD httpfs")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    bucket = "s3://" + source.removeprefix("s3://").split("/")[0]
    # AWS's public bucket is read anonymously: a request signed by an instance role is refused unless the role's
    # own policy grants that bucket, public or not. Any other source uses the machine's credentials.
    auth = "PROVIDER config" if bucket == "s3://redshift-downloads" else "PROVIDER credential_chain"
    con.execute(f"CREATE OR REPLACE SECRET tpcds_source (TYPE s3, {auth}, REGION '{region}', SCOPE '{bucket}')")
    for table, cols in COLUMNS.items():
        types = schema.execute(f"DESCRIBE {table}").fetchall()
        spec = "{" + ", ".join(f"'{name}': '{kind}'" for name, kind, *_ in types) + "}"
        t = time.time()
        where = _store_sales_filter() if table == "store_sales" else ""
        _copy(con, f"""
            SELECT {cols} FROM read_csv('{source.rstrip("/")}/{table}/*.dat.gz', delim='|', header=false,
                                        columns={spec}, nullstr='', quote='', escape='', auto_detect=false){where}""",
              base / f"{table}.parquet")
        print(f"  {table}: {_count(con, base / f'{table}.parquet'):,} rows, {time.time() - t:.0f}s")
    schema.close()


def generate_base(con, sf: float, data: Path, source: str | None = None) -> None:
    base = data / "landing" / "base"
    if all((base / f"{t}.parquet").exists() for t in COLUMNS):
        print(f"base already at {base}")
        return
    if source:
        convert_base(con, source, data)
        return
    gen_db = data / "_dsdgen.duckdb"
    gen = duckdb.connect(str(gen_db))
    t = time.time()
    gen.execute(f"INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf={sf})")
    print(f"dsdgen sf={sf}: {time.time() - t:.0f}s")
    for table, cols in COLUMNS.items():
        where = _store_sales_filter() if table == "store_sales" else ""
        _copy(gen, f"SELECT {cols} FROM {table}{where}", base / f"{table}.parquet")
        print(f"  {table}: {_count(gen, base / f'{table}.parquet'):,} rows")
    gen.close()
    gen_db.unlink()
    Path(f"{gen_db}.wal").unlink(missing_ok=True)


def _bucket(keys: str, k: int, salt: str) -> str:
    """A deterministic bucket in [0, PPM) per row, per batch."""
    return f"(hash({keys}, {k}, '{salt}') % {PPM})"


def generate_batch(con, data: Path, k: int, scale: float, scenario: str = "mix") -> dict:
    prev = data / "snapshot" / f"v{k - 1}"
    cur = data / "snapshot" / f"v{k}"
    land = data / "landing" / f"batch_{k:03d}"
    if (cur / "store_sales.parquet").exists() and (land / "store_sales.parquet").exists():
        print(f"batch {k} already generated")
        return json.loads((land / "_batch.json").read_text())
    cur.mkdir(parents=True, exist_ok=True)
    land.mkdir(parents=True, exist_ok=True)
    fr = {name: f * scale * SCENARIOS[scenario].get(name, 1) for name, f in FRACTIONS.items()}

    def src(table):
        return f"read_parquet('{prev / f'{table}.parquet'}')"

    # Fact changes: one hash bucket per row splits the fact into disjoint update, delete and insert-source
    # slices, so no row is both corrected and voided in one batch.
    u, d, i = (round(fr[n] * PPM) for n in ("sales_update", "sales_delete", "sales_insert"))
    h = _bucket("ss_ticket_number, ss_item_sk", k, "sales")
    con.execute(f"CREATE OR REPLACE TEMP VIEW s AS SELECT *, {h} AS _h FROM {src('store_sales')}")
    con.execute(f"CREATE OR REPLACE TEMP VIEW r AS SELECT * FROM {src('store_returns')}")
    max_ticket = con.execute("SELECT max(ss_ticket_number) FROM s").fetchall()[0][0]
    cols = COLUMNS["store_sales"]
    # A correction: one more unit sold, with the amounts rescaled to match.
    corrected = (
        "ss_ticket_number, ss_item_sk, ss_sold_date_sk, ss_store_sk, ss_customer_sk, ss_quantity + 1, "
        "ss_sales_price, "
        "CAST(round(ss_ext_sales_price * (ss_quantity + 1) / ss_quantity, 2) AS DECIMAL(7,2)), "
        "CAST(round(ss_net_paid * (ss_quantity + 1) / ss_quantity, 2) AS DECIMAL(7,2)), "
        "CAST(round(ss_net_profit * (ss_quantity + 1) / ss_quantity, 2) AS DECIMAL(7,2))"
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE upd_before AS
        SELECT {cols} FROM s WHERE _h < {u} AND ss_quantity BETWEEN 1 AND 98""")
    con.execute(f"CREATE OR REPLACE TEMP TABLE upd_after AS SELECT {corrected} FROM upd_before")
    # Voided sales: never one that has been returned, so no return is orphaned.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE del AS
        SELECT {cols} FROM s WHERE _h >= {u} AND _h < {u + d}
          AND NOT EXISTS (SELECT 1 FROM r WHERE r.sr_ticket_number = s.ss_ticket_number
                                            AND r.sr_item_sk = s.ss_item_sk)""")
    # New sales: copies of existing lines on new tickets past the highest ticket so far.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ins AS
        SELECT ss_ticket_number + {max_ticket} AS ss_ticket_number, * EXCLUDE (ss_ticket_number, _h)
        FROM s WHERE _h >= {u + d} AND _h < {u + d + i}""")
    con.execute(f"CREATE OR REPLACE TEMP TABLE ins AS SELECT {cols} FROM ins")
    # New returns: a full refund of a sale not yet returned and not voided or corrected in this batch.
    rr = round(fr["returns_insert"] * PPM)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE ret AS
        SELECT ss_ticket_number AS sr_ticket_number, ss_item_sk AS sr_item_sk,
               ss_sold_date_sk + 14 AS sr_returned_date_sk, ss_quantity AS sr_return_quantity,
               ss_net_paid AS sr_return_amt, CAST(round(ss_net_paid * 0.1, 2) AS DECIMAL(7,2)) AS sr_net_loss
        FROM s WHERE _h >= {u + d + i} AND _h < {u + d + i + rr}
          AND NOT EXISTS (SELECT 1 FROM r WHERE r.sr_ticket_number = s.ss_ticket_number
                                            AND r.sr_item_sk = s.ss_item_sk)""")

    # The CDC feed: before images of corrected and voided rows, after images of corrected and new rows.
    _copy(con, """
        SELECT *, 'before' AS _change FROM upd_before
        UNION ALL SELECT *, 'after' FROM upd_after
        UNION ALL SELECT *, 'before' FROM del
        UNION ALL SELECT *, 'after' FROM ins""", land / "store_sales.parquet")
    _copy(con, "SELECT * FROM ret", land / "store_returns.parquet")

    # Dimension changes, landed as full extracts. Reclassified items move to the next category along and
    # change price; moved customers take another existing address.
    iu = round(fr["item_update"] * PPM)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE cats AS
        SELECT i_category AS c, row_number() OVER (ORDER BY i_category) AS n, count(*) OVER () AS m
        FROM (SELECT DISTINCT i_category FROM {src('item')} WHERE i_category IS NOT NULL)""")
    item_changed = con.execute(f"""
        SELECT count(*) FROM {src('item')} WHERE {_bucket('i_item_sk', k, 'item')} < {iu}""").fetchall()[0][0]
    _copy(con, f"""
        SELECT i.i_item_sk, i.i_item_id, i.i_brand, i.i_class,
               CASE WHEN {_bucket('i.i_item_sk', k, 'item')} < {iu}
                    THEN coalesce((SELECT c2.c FROM cats c1 JOIN cats c2 ON c2.n = c1.n % c1.m + 1
                                   WHERE c1.c = i.i_category), i.i_category)
                    ELSE i.i_category END AS i_category,
               CASE WHEN {_bucket('i.i_item_sk', k, 'item')} < {iu}
                    THEN CAST(round(i.i_current_price * 1.1, 2) AS DECIMAL(7,2))
                    ELSE i.i_current_price END AS i_current_price
        FROM {src('item')} i""", land / "item.parquet")
    cu = round(fr["customer_update"] * PPM)
    n_addr = con.execute(f"SELECT max(ca_address_sk) FROM {src('customer_address')}").fetchall()[0][0]
    cust_changed = con.execute(f"""
        SELECT count(*) FROM {src('customer')} WHERE {_bucket('c_customer_sk', k, 'cust')} < {cu}""").fetchall()[0][0]
    _copy(con, f"""
        SELECT c_customer_sk, c_customer_id,
               CASE WHEN {_bucket('c_customer_sk', k, 'cust')} < {cu}
                    THEN (coalesce(c_current_addr_sk, 0) + 7919 * {k}) % {n_addr} + 1
                    ELSE c_current_addr_sk END AS c_current_addr_sk,
               c_birth_year, c_preferred_cust_flag
        FROM {src('customer')}""", land / "customer.parquet")

    # The snapshot after the batch, for the baselines.
    _copy(con, f"""
        SELECT {cols} FROM s
        WHERE (ss_ticket_number, ss_item_sk) NOT IN (SELECT (ss_ticket_number, ss_item_sk) FROM upd_before)
          AND (ss_ticket_number, ss_item_sk) NOT IN (SELECT (ss_ticket_number, ss_item_sk) FROM del)
        UNION ALL SELECT * FROM upd_after
        UNION ALL SELECT * FROM ins""", cur / "store_sales.parquet")
    _copy(con, "SELECT * FROM r UNION ALL SELECT * FROM ret", cur / "store_returns.parquet")
    shutil.copyfile(land / "item.parquet", cur / "item.parquet")
    shutil.copyfile(land / "customer.parquet", cur / "customer.parquet")
    for table in ("store", "date_dim", "customer_address"):
        link = cur / f"{table}.parquet"
        if not link.exists():
            os.link(prev / f"{table}.parquet", link)

    counts = {name: con.execute(f"SELECT count(*) FROM {name}").fetchall()[0][0]
              for name in ("upd_before", "del", "ins", "ret")}
    batch = {
        "batch": k,
        "scenario": scenario,
        "scale": scale,
        "sales_updated": counts["upd_before"],
        "sales_deleted": counts["del"],
        "sales_inserted": counts["ins"],
        "returns_inserted": counts["ret"],
        "items_reclassified": item_changed,
        "customers_moved": cust_changed,
    }
    (land / "_batch.json").write_text(json.dumps(batch, indent=2))
    return batch


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", type=float, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--batches", type=int, default=1)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="mix")
    ap.add_argument("--change", type=float, default=1.0, help="scale every change fraction")
    ap.add_argument("--size", type=int, help="about this many new sales per batch, whatever the scale factor")
    ap.add_argument("--base-from", type=Path, help="hard-link this data directory's base instead of generating")
    ap.add_argument("--source", help="build the base from pre-generated TPC-DS text at this location")
    args = ap.parse_args()
    data = args.data.resolve()
    data.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{data / '_tmp'}'")
    if args.base_from:
        base = data / "landing" / "base"
        base.mkdir(parents=True, exist_ok=True)
        for table in COLUMNS:
            if not (base / f"{table}.parquet").exists():
                os.link(args.base_from.resolve() / "landing" / "base" / f"{table}.parquet", base / f"{table}.parquet")
    generate_base(con, args.sf, data, args.source)
    change = args.change
    if args.size:
        n_sales = _count(con, data / "landing" / "base" / "store_sales.parquet")
        per_insert = FRACTIONS["sales_insert"] * SCENARIOS[args.scenario].get("sales_insert", 1)
        change = args.size / (n_sales * per_insert)
    v0 = data / "snapshot" / "v0"
    v0.mkdir(parents=True, exist_ok=True)
    for table in COLUMNS:
        link = v0 / f"{table}.parquet"
        if not link.exists():
            os.link(data / "landing" / "base" / f"{table}.parquet", link)

    batches = []
    for k in range(1, args.batches + 1):
        t = time.time()
        batches.append(generate_batch(con, data, k, change, args.scenario))
        print(f"batch {k}: {time.time() - t:.0f}s {batches[-1]}")

    rows = {t: _count(con, data / "landing" / "base" / f"{t}.parquet") for t in COLUMNS}
    (data / "manifest.json").write_text(json.dumps(
        {"sf": args.sf, "scenario": args.scenario, "duckdb": duckdb.__version__, "source": args.source or "dsdgen",
         "base_rows": rows,
         "batches": batches}, indent=2))
    shutil.rmtree(data / "_tmp", ignore_errors=True)


if __name__ == "__main__":
    main()
