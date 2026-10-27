#!/bin/bash
# Spark on the standalone cluster node.sh started (this is the master node, which also runs a worker and the
# driver). Waits for every worker, then runs each scenario's rebuild REPS times and checks it against REF_RUN's
# first DuckDB rebuild (a single.sh run's id). Set by the job that sources this: SF, WORKERS, REF_RUN, EXEC_MEM,
# EXEC_CORES, DRIVER_MEM, and optionally SCENARIOS, REPS, DATA_SUFFIX (as in single.sh).
set -uxo pipefail
SCENARIOS=${SCENARIOS:-mix fact append}
REPS=${REPS:-3}
OUT=s3://$BUCKET/runs/$RUN
MASTER_IP=${SPARK_MASTER#spark://}; MASTER_IP=${MASTER_IP%:7077}
until [ "$(curl -s "http://$MASTER_IP:8080/json/" | python -c 'import json,sys; print(json.load(sys.stdin).get("aliveworkers", 0))')" -ge "$WORKERS" ]; do
  sleep 5
done
curl -s "http://$MASTER_IP:8080/json/" > /root/out/cluster.json
for sc in $SCENARIOS; do
  for rep in $(seq 1 "$REPS"); do
    tag=sparkcluster_${sc}_$rep
    echo "=== $(date -u +%T) $tag"
    python resources.py --interval 0.5 --out "/root/out/res_$tag.json" -- \
      python run_spark.py --data "s3://$BUCKET/data/sf${SF}_$sc${DATA_SUFFIX:-}" --version 1 --out "$OUT/$tag" --master "$SPARK_MASTER" \
      --driver-memory "$DRIVER_MEM" --executor-memory "$EXEC_MEM" --executor-cores "$EXEC_CORES" \
      --scratch /mnt/nvme/spark-local > "/root/out/$tag.json"
    echo "exit $? $tag" >> /root/out/exits.txt
    python verify.py "$OUT/$tag" "s3://$BUCKET/runs/$REF_RUN/duckdb_${sc}_1" > "/root/out/verify_$tag.txt" 2>&1
    echo "verify $? $tag" >> /root/out/exits.txt
    [ "$rep" != 1 ] && aws s3 rm --quiet --recursive "$OUT/$tag"
  done
done
