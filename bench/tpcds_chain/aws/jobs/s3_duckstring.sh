#!/bin/bash
# Repeat s3_single.sh's Duckstring run (first build and the mixed change, SF100, from S3) twice, to catch why its
# change failed once: run_duckstring.py now prints a failed run's tracebacks. Sampled like the other S3 runs.
set -uxo pipefail
DATA=s3://$BUCKET/data/sf100_mix
OUT=s3://$BUCKET/runs/$RUN
export DUCKSTRING_RELAY=off
log() { echo "$1 $2 $(date +%s.%N)" >> /root/out/runs.tsv; }
for r in 1 2; do
  tag=duckstring_$r
  log "$tag" start
  python run_duckstring.py --data "$DATA" --root /mnt/nvme/ds --data-root "$OUT/$tag" --results "/root/out/runs_$tag.json" \
    > "/root/out/$tag.json" 2> "/root/out/$tag.err"
  echo "exit $? $tag" >> /root/out/exits.txt
  log "$tag" end
  cp /mnt/nvme/ds.log "/root/out/catchment_$tag.log" 2>/dev/null
  cp -r /mnt/nvme/ds/ponds/dimensions/m1/pond.db "/root/out/dimensions_pond_$tag.db" 2>/dev/null
  python -c "import sqlite3; print(sqlite3.connect('/mnt/nvme/ds/duck.db').execute(\"SELECT * FROM ripple_run WHERE status = 'failed'\").fetchall())" \
    > "/root/out/failed_$tag.txt" 2>&1
  python verify.py "duckstring:$OUT/$tag" "$(aws s3 cp --quiet "s3://$BUCKET/runs/ref_s3" -)" > "/root/out/verify_$tag.txt" 2>&1
  echo "verify $? $tag" >> /root/out/exits.txt
  sleep 10
done
