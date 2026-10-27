#!/bin/bash
# SF1000 DuckDB on one r7gd.8xlarge, on the split-file snapshots StarRocks read, for a like-for-like comparison.
export SF=1000 ENGINES=duckdb REPS=1 DATA_SUFFIX=chunked REF_RUN=20261024T060655Z-sf1000_shakedown
bash aws/jobs/single.sh
