#!/bin/bash
# How much of each engine's time on one machine (m7gd.8xlarge) goes on S3, at SF100, mixed scenario. Regenerates the
# data from the public text, measures the machine's S3 ceiling (parallel ranged reads, 32/64/128 threads), then runs
# DuckDB from S3 and from local NVMe (twice each, the second with DuckDB's profiler), Duckstring, Spark and StarRocks
# from S3. node.sh's sampler records CPU and network each second; runs.tsv has each run's start and end.
set -uxo pipefail
SRC=s3://redshift-downloads/TPC-DS/2.13/100GB
D=/mnt/nvme/data/sf100_mix
DATA=s3://$BUCKET/data/sf100_mix
OUT=s3://$BUCKET/runs/$RUN
export DUCKSTRING_RELAY=off
log() { echo "$1 $2 $(date +%s.%N)" >> /root/out/runs.tsv; }
run() {  # tag, command...: timed, logged, its JSON line kept
  local tag=$1; shift
  log "$tag" start; "$@" > "/root/out/$tag.json" 2> "/root/out/$tag.err"; echo "exit $? $tag" >> /root/out/exits.txt
  log "$tag" end; sleep 10
}
python gen.py --sf 100 --data "$D" --scenario mix --source "$SRC" > /root/out/gen.log 2>&1
aws s3 sync --quiet --exclude "landing/RELEASED" --exclude "_tmp/*" --exclude "snapshot/v0/*" "$D" "$DATA/"
for th in 32 64 128; do
  run "ceiling_$th" python aws/s3_ceiling.py "$DATA/snapshot/v1/" --threads "$th" --seconds 20
done
run duckdb_s3_1 python run_duckdb.py --data "$DATA" --version 1 --out "$OUT/duckdb_s3_1" --scratch /mnt/nvme/spill
run duckdb_s3_2 python run_duckdb.py --data "$DATA" --version 1 --out "$OUT/duckdb_s3_2" --scratch /mnt/nvme/spill --profile
for r in 1 2; do
  sync; echo 3 > /proc/sys/vm/drop_caches  # read from the NVMe, not the page cache the generator just filled
  p=; [ "$r" = 2 ] && p=--profile
  run "duckdb_local_$r" python run_duckdb.py --data "$D" --version 1 --out "/mnt/nvme/out_local_$r" --scratch /mnt/nvme/spill $p
done
run duckstring_1 python run_duckstring.py --data "$DATA" --root /mnt/nvme/ds --data-root "$OUT/duckstring_1" \
  --results /root/out/runs_duckstring_1.json
cp /mnt/nvme/ds.log /root/out/catchment_duckstring_1.log 2>/dev/null
run spark_s3_1 python run_spark.py --data "$DATA" --version 1 --out "$OUT/spark_s3_1" --driver-memory 96g \
  --scratch /mnt/nvme/spark-local
bash aws/starrocks_up.sh
run starrocks_s3_1 python run_starrocks.py --data "$DATA" --version 1 --out "$OUT/starrocks_s3_1"
REF=$OUT/duckdb_s3_1
for t in "$OUT/duckdb_s3_2" /mnt/nvme/out_local_1 /mnt/nvme/out_local_2 "duckstring:$OUT/duckstring_1" "$OUT/spark_s3_1" \
         "$OUT/starrocks_s3_1"; do
  name=$(basename "${t#duckstring:}")
  python verify.py "$t" "$REF" > "/root/out/verify_$name.txt" 2>&1; echo "verify $? $name" >> /root/out/exits.txt
done
aws s3 cp --quiet - "s3://$BUCKET/runs/ref_s3" <<< "$REF"
