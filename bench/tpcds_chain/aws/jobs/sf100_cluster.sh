#!/bin/bash
# SF100 Spark on four m7gd.2xlarge (8 vCPU, 32 GB each: together, the single machine's 32 vCPU and 128 GB).
# Outputs are checked against the DuckDB rebuild of the single-machine run named in s3://$BUCKET/runs/ref_sf100.
export SF=100 WORKERS=4 EXEC_MEM=20g EXEC_CORES=8 DRIVER_MEM=6g
export REF_RUN=$(aws s3 cp --quiet "s3://$BUCKET/runs/ref_sf100" -)
bash aws/jobs/cluster.sh
