#!/bin/bash
# StarRocks across the cluster's nodes: a frontend and a backend here (the master node), a backend on each worker
# (aws/sr_worker.sh). The all-in-one image can't be used for this: its frontend calls itself 127.0.0.1, and a
# backend on another machine refuses its heartbeats. Registers every backend, waits until all are alive, then
# runs single.sh with the starrockscluster engine. Set by the job that sources this: SF, WORKERS (the number of
# worker nodes), REF_RUN, and optionally SCENARIOS, REPS.
set -euxo pipefail
bash aws/sr_frontend.sh
if [ -n "${CHUNKED:-}" ]; then  # the split-file snapshots (chunk_copy.py), made once and kept
  for sc in ${SCENARIOS:-mix fact append}; do
    python chunk_copy.py "s3://$BUCKET/data/sf${SF}_$sc/snapshot/v1" "s3://$BUCKET/data/sf${SF}_${sc}chunked/snapshot/v1"
  done
  export DATA_SUFFIX=chunked
fi
export ENGINES=starrockscluster
bash aws/jobs/single.sh || true  # its exit status is its last test's, not a failure

# Diagnostic: does StarRocks read one large Parquet file with less parallelism than many? A copy of the mixed
# snapshot with the two fact tables as 256 MB files, run once and checked against the same DuckDB rebuild.
if [ -n "${CHUNK_TEST:-}" ]; then
  CH=s3://$BUCKET/data/sf${SF}_mixchunked/snapshot/v1
  python - <<PY
import duckdb, paths
src = "s3://$BUCKET/data/sf${SF}_mix/snapshot/v1"
con = duckdb.connect(); paths.duckdb_setup(con, src)
for t in ("store_sales", "store_returns", "item", "store", "date_dim", "customer", "customer_address"):
    big = t in ("store_sales", "store_returns")
    target = f"$CH/{t}" if big else f"$CH/{t}.parquet"
    opts = "FORMAT parquet, COMPRESSION zstd" + (", FILE_SIZE_BYTES '256MB'" if big else "")
    con.execute(f"COPY (SELECT * FROM read_parquet('{src}/{t}.parquet')) TO '{target}' ({opts})")
PY
  tag=starrockscluster_mixchunked_1
  python resources.py --interval 0.5 --out "/root/out/res_$tag.json" -- \
    python run_starrocks.py --data "s3://$BUCKET/data/sf${SF}_mixchunked" --version 1 --out "s3://$BUCKET/runs/$RUN/$tag" \
    > "/root/out/$tag.json"
  echo "exit $? $tag" >> /root/out/exits.txt
  python verify.py "s3://$BUCKET/runs/$RUN/$tag" "s3://$BUCKET/runs/$REF_RUN/duckdb_mix_1" > "/root/out/verify_$tag.txt" 2>&1
  echo "verify $? $tag" >> /root/out/exits.txt
fi
