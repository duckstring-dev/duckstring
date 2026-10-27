#!/bin/bash
# The cluster half of the S3 measurements: four m7gd.2xlarge running Spark, then StarRocks, at SF100 (the data and
# DuckDB reference s3_single.sh left). This is the master node: a Spark master and worker, a StarRocks frontend and
# backend. First every node reads S3 at full speed at the same moment (the cluster's ceiling), then each engine runs
# once. Every node's sampler records CPU and network; runs.tsv here has each run's start and end.
set -uxo pipefail
export WORKERS=3
IP=$(hostname -I | awk '{print $1}')
DATA=s3://$BUCKET/data/sf100_mix
OUT=s3://$BUCKET/runs/$RUN
REF=$(aws s3 cp --quiet "s3://$BUCKET/runs/ref_s3" -)
log() { echo "$1 $2 $(date +%s.%N)" >> /root/out/runs.tsv; }
SPARK_MASTER_HOST=$IP "$SPARK_HOME/sbin/start-master.sh"
"$SPARK_HOME/sbin/start-worker.sh" "spark://$IP:7077"
aws s3 cp --quiet - "$RESULTS/master" <<< "$IP"
bash aws/sr_frontend.sh  # waits until all four StarRocks backends are alive
until [ "$(curl -s "http://$IP:8080/json/" | python -c 'import json,sys; print(json.load(sys.stdin).get("aliveworkers", 0))')" -ge 4 ]; do
  sleep 5
done
START=$(( $(date +%s) + 20 ))
aws s3 cp --quiet - "$RESULTS/ceiling_start" <<< "$START"
log ceiling start
python aws/s3_ceiling.py "$DATA/snapshot/v1/" --threads 64 --seconds 20 --start "$START" > /root/out/ceiling.json
log ceiling end
sleep 15  # let the workers' reads finish
log sparkcluster_1 start
python run_spark.py --data "$DATA" --version 1 --out "$OUT/sparkcluster_1" --master "spark://$IP:7077" \
  --driver-memory 6g --executor-memory 20g --executor-cores 8 --scratch /mnt/nvme/spark-local \
  > /root/out/sparkcluster_1.json 2> /root/out/sparkcluster_1.err
echo "exit $? sparkcluster_1" >> /root/out/exits.txt
log sparkcluster_1 end
sleep 10
log starrockscluster_1 start
python run_starrocks.py --data "$DATA" --version 1 --out "$OUT/starrockscluster_1" > /root/out/starrockscluster_1.json \
  2> /root/out/starrockscluster_1.err
echo "exit $? starrockscluster_1" >> /root/out/exits.txt
log starrockscluster_1 end
for t in sparkcluster_1 starrockscluster_1; do
  python verify.py "$OUT/$t" "$REF" > "/root/out/verify_$t.txt" 2>&1; echo "verify $? $t" >> /root/out/exits.txt
done
