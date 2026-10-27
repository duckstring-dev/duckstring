#!/bin/bash
# Confirm the departing-Duck fix (duckstring 6750d146) where it failed: Duckstring's first build and the mixed change
# from S3 at SF100, twice on a fresh machine, each checked against a DuckDB rebuild run afterwards.
set -uxo pipefail
SRC=s3://redshift-downloads/TPC-DS/2.13/100GB
D=/mnt/nvme/data/sf100_mix
DATA=s3://$BUCKET/data/sf100_mix
OUT=s3://$BUCKET/runs/$RUN
export DUCKSTRING_RELAY=off
python gen.py --sf 100 --data "$D" --scenario mix --source "$SRC" > /root/out/gen.log 2>&1
aws s3 sync --quiet --exclude "landing/RELEASED" --exclude "_tmp/*" --exclude "snapshot/v0/*" "$D" "$DATA/"
for r in 1 2; do
  python run_duckstring.py --data "$DATA" --root /mnt/nvme/ds --data-root "$OUT/duckstring_$r" \
    --results "/root/out/runs_duckstring_$r.json" > "/root/out/duckstring_$r.json" 2> "/root/out/duckstring_$r.err"
  echo "exit $? duckstring_$r" >> /root/out/exits.txt
  cp /mnt/nvme/ds.log "/root/out/catchment_duckstring_$r.log" 2>/dev/null
  sleep 10
done
python run_duckdb.py --data "$DATA" --version 1 --out "$OUT/duckdb_ref" --scratch /mnt/nvme/spill > /root/out/duckdb_ref.json
for r in 1 2; do
  python verify.py "duckstring:$OUT/duckstring_$r" "$OUT/duckdb_ref" > "/root/out/verify_duckstring_$r.txt" 2>&1
  echo "verify $? duckstring_$r" >> /root/out/exits.txt
done
