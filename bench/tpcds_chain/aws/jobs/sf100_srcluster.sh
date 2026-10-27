#!/bin/bash
# SF100 StarRocks on four m7gd.2xlarge (a frontend and four backends; together the single machine's 32 vCPU and
# 128 GB), checked against the SF100 single-machine run's DuckDB rebuilds.
export SF=100 WORKERS=3 CHUNK_TEST=1 REF_RUN=$(aws s3 cp --quiet "s3://$BUCKET/runs/ref_sf100" -)
bash aws/jobs/sr_cluster.sh
