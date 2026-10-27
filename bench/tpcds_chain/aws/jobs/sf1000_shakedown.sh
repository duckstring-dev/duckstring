#!/bin/bash
# The 1 TB shakedown on one r7gd.8xlarge: build SF1000 from the public text, then each scenario once with
# DuckDB and Duckstring (Spark at 1 TB is left to published benchmarks).
set -e
export SF=1000 SRC=s3://redshift-downloads/TPC-DS/2.13/1TB
bash aws/jobs/prep.sh
export ENGINES="duckdb duckstring" REPS=1
bash aws/jobs/single.sh
