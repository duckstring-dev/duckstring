# TPC-DS chain benchmark

A pipeline derived from TPC-DS's store sales, built once and then updated with a change of about 0.5%, in
Duckstring (Trickle mode), plain DuckDB and Spark. Every run's output is checked against the others.

## The chain

```
sales_feed (store_sales by CDC, store_returns)  ┐
dimensions (item, store, date_dim, customer,    ├─> sales_enriched ─┬─> category_month
            customer_address)                   ┘                   ├─> store_quarter
                                                                    └─> customer_value
```

- `sales_enriched` joins each sale to its date, item and store, left joins its customer, the customer's
  address and any return, and computes the revenue net of returns.
- The three Outlets aggregate it: by category and month, by store and quarter, and by customer and state.
- `chain_sql.py` is the chain as plain SQL. `ponds/` is the same chain as Duckstring Ponds, using the
  Trickle builder and incremental aggregates.

## The change

`gen.py` generates the base with DuckDB's `tpcds` extension and keeps only the columns the chain reads. Each
change batch touches about 0.5% of the enriched sales:

- 0.2% new sales.
- 0.05% corrected sales (one more unit, amounts rescaled).
- 0.02% voided sales.
- 0.03% newly returned sales.
- 0.05% of items moved to another category, which changes all of their sales.
- 0.1% of customers moved to another address, which changes all of theirs.

Duckstring is fed the way a production pipeline would be. `store_sales` arrives as a CDC feed with full
before and after row images (MySQL's binlog default), returns are append-only, and the dimensions arrive as
full nightly extracts. The baselines rebuild from a full snapshot after each batch. `--change` scales every
fraction, for a run time against change size curve.

## Running it

```bash
PY=/path/to/duckstring/.venv/bin/python   # Duckstring, with DuckDB 2.0
$PY gen.py --sf 10 --data /data/sf10 --batches 2

$PY run_duckdb.py --data /data/sf10 --version 0 --out /out/duckdb_v0
$PY run_duckdb.py --data /data/sf10 --version 2 --out /out/duckdb_v2

$PY run_duckstring.py --data /data/sf10 --root /out/ds --batches 2 [--warm] --results ds.json
$PY verify.py duckstring:/out/ds /out/duckdb_v2

JAVA_HOME=/path/to/jdk17 /path/to/spark-venv/bin/python run_spark.py --data /data/sf10 --version 2 --out /out/spark_v2
$PY verify.py /out/spark_v2 /out/duckdb_v2
```

`run_duckstring.py` starts its own Catchment on `--root` (deleted first), so it never touches a registered
one. Each phase is timed from the Refresh request until every Pond has finished a Run at least as fresh as
the request. `--warm` keeps the Ducks running between phases. `--continuous K --every S` then releases K more batches S seconds
apart under a continuous Refresh, and `--status-log` records the Catchment's status every 0.25 s.

## On AWS

The AWS runs (`results/aws/`) used `aws/`: `build.sh` packages Duckstring and Trickle wheels from a checkout plus
this directory to `s3://$BUCKET/code/{commit}/`, and `launch.sh` starts nodes that run a job from `aws/jobs/`,
write their results to `s3://$BUCKET/results/{run}/` and terminate themselves. You need, in us-east-1:

- a bucket for code and results (`BUCKET`);
- a security group whose members can reach each other, with nothing inbound (`SG`);
- an instance profile that can read and write the bucket (`PROFILE`, default `duckstring-catchment`).

```bash
export BUCKET=my-bench-bucket SG=sg-0123456789abcdef0
aws/build.sh /path/to/duckstring                  # prints the code prefix
aws/launch.sh --type m7gd.8xlarge --job sf100_rerun --code s3://$BUCKET/code/abc1234
```

`prep.sh` builds the data from the TPC-DS text AWS publishes (`s3://redshift-downloads/TPC-DS/2.13/`), and each job
lists what it runs. `report_run.py` draws one Duckstring run's timeline from its records.

## Disk

Roughly, for SF *n*: the base is 0.44*n* GB, each snapshot the same again, and the Duckstring root about
0.57*n* GB. Generation briefly needs about 3.3*n* GB for `dsdgen`'s database.
