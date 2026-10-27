#!/bin/bash
# The new-sales change at 1 TB on one r7gd.8xlarge, after plans/trickle-insert-only.md: build SF1000's append
# scenario from the public text, then DuckDB's rebuild and Duckstring's first build and change, then a second
# Duckstring run with the Ducks timed step by step (duckstring_prof in single.sh; summarise with prof/buckets.py
# and prof/report.py).
set -euxo pipefail
SF=1000
SRC=s3://redshift-downloads/TPC-DS/2.13/1TB
D=/mnt/nvme/data
python gen.py --sf "$SF" --data "$D/sf${SF}_append" --scenario append --source "$SRC" 2>&1 | tee /root/out/gen_append.log
cp "$D/sf${SF}_append/manifest.json" /root/out/manifest_append.json
aws s3 sync --quiet --exclude "landing/RELEASED" --exclude "_tmp/*" --exclude "snapshot/v0/*" \
  "$D/sf${SF}_append" "s3://$BUCKET/data/sf${SF}_append/"
rm -rf "$D"  # the runs read S3, as before
export SF SCENARIOS=append ENGINES="duckdb duckstring duckstring_prof" REPS=1
bash aws/jobs/single.sh
