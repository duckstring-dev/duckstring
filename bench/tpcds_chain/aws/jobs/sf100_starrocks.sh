#!/bin/bash
# SF100 StarRocks on one machine (m7gd.8xlarge), checked against the SF100 single-machine run's DuckDB rebuilds.
set -e
bash aws/starrocks_up.sh
export SF=100 ENGINES=starrocks REF_RUN=$(aws s3 cp --quiet "s3://$BUCKET/runs/ref_sf100" -)
bash aws/jobs/single.sh
