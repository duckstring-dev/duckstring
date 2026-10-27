#!/bin/bash
# A worker for s3_cluster.sh: a Spark worker and a StarRocks backend, and its part of the simultaneous S3 read.
set -uxo pipefail
until MASTER=$(aws s3 cp --quiet "$RESULTS/master" - 2>/dev/null); do sleep 5; done
"$SPARK_HOME/sbin/start-worker.sh" "spark://$MASTER:7077"
bash aws/sr_backend.sh
until START=$(aws s3 cp --quiet "$RESULTS/ceiling_start" - 2>/dev/null); do sleep 2; done
python aws/s3_ceiling.py "s3://$BUCKET/data/sf100_mix/snapshot/v1/" --threads 64 --seconds 20 --start "$START" \
  > /root/out/ceiling.json
until aws s3 ls "$RESULTS/done" >/dev/null 2>&1; do sleep 15; done
