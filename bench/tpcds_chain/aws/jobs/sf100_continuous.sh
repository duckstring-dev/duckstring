#!/bin/bash
# The continuous set from sf100_rerun.sh alone (Duckstring with slots on and off), against that run's DuckDB
# rebuild of v8 (s3://$BUCKET/runs/ref_sf100_stress): its first attempt failed on a harness race reading the
# release marker on S3.
set -uxo pipefail
REF_RUN=$(aws s3 cp "s3://$BUCKET/runs/ref_sf100_stress" - | tr -d '[:space:]')
[ -n "$REF_RUN" ] || { echo "no reference run: run sf100_rerun first"; exit 1; }
export SF=100 REF_RUN
SCENARIOS=mix8 ENGINES="duckstring duckstring_off" REPS=1 VERSION=8 DS_ARGS="--continuous 7 --every 10" \
  bash aws/jobs/single.sh
