#!/bin/bash
# StarRocks on one machine with declared schemas: the mixed scenario twice, to compare with inference.
set -e
bash aws/starrocks_up.sh
export SF=100 ENGINES=starrocks SCENARIOS=mix REPS=2 REF_RUN=$(aws s3 cp --quiet "s3://$BUCKET/runs/ref_sf100" -)
bash aws/jobs/single.sh || true
