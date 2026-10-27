#!/bin/bash
# SF100 again on one m7gd.8xlarge after plans/trickle-insert-only.md and plans/memory-budget.md, and the dress
# rehearsal for SF1000. Builds the data (the bucket was emptied), plus a mixed set with eight change batches
# for the continuous runs (sf100_mix8: only its base, batches and final snapshot v8 are uploaded). Then:
#
#   1. the three scenarios, three repeats: DuckDB, Duckstring (slots on, N=4) and Duckstring with slots off;
#   2. one profiled Duckstring run of each scenario, kept out of the timings;
#   3. the continuous set: DuckDB's rebuild of v8 (the reference for this and the stress nodes, recorded at
#      s3://$BUCKET/runs/ref_sf100_stress), then Duckstring with slots on and off: the first build, batch 1 as a
#      phase, then batches 2 to 8 released every 10 s under a continuous Refresh.
set -uxo pipefail
SF=100
SRC=s3://redshift-downloads/TPC-DS/2.13/100GB
D=/mnt/nvme/data
bash aws/jobs/prep.sh
python gen.py --sf "$SF" --data "$D/sf${SF}_mix8" --scenario mix --batches 8 --base-from "$D/sf${SF}_mix" \
  2>&1 | tee /root/out/gen_mix8.log
cp "$D/sf${SF}_mix8/manifest.json" /root/out/manifest_mix8.json
aws s3 sync --quiet --exclude "landing/RELEASED" --exclude "_tmp/*" --exclude "snapshot/v[0-7]/*" \
  "$D/sf${SF}_mix8" "s3://$BUCKET/data/sf${SF}_mix8/"
rm -rf "$D"  # the runs read S3, as before
export SF
SCENARIOS="append fact mix" ENGINES="duckdb duckstring duckstring_off" REPS=3 bash aws/jobs/single.sh
SCENARIOS="append fact mix" ENGINES="duckstring_prof" REPS=1 bash aws/jobs/single.sh
SCENARIOS=mix8 ENGINES="duckdb duckstring duckstring_off" REPS=1 VERSION=8 DS_ARGS="--continuous 7 --every 10" \
  bash aws/jobs/single.sh
aws s3 cp --quiet - "s3://$BUCKET/runs/ref_sf100_stress" <<< "$RUN"
