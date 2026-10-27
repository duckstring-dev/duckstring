#!/bin/bash
# The memory stress run on a small machine (c7gd.2xlarge: 8 vCPU, 16 GB), one Duckstring setting per machine
# (ENGINE, set by the sf100_stress_{setting}.sh wrappers): the continuous set from sf100_rerun.sh (first build,
# batch 1 as a phase, batches 2 to 8 every 10 s under a continuous Refresh), checked against that run's DuckDB
# rebuild of v8. Needs sf100_rerun.sh to have finished.
set -uxo pipefail
REF_RUN=$(aws s3 cp "s3://$BUCKET/runs/ref_sf100_stress" - | tr -d '[:space:]')
[ -n "$REF_RUN" ] || { echo "no reference run: run sf100_rerun first"; exit 1; }
export SF=100 REF_RUN
SCENARIOS=mix8 ENGINES="$ENGINE" REPS=1 VERSION=8 DS_ARGS="--continuous 7 --every 10" bash aws/jobs/single.sh
