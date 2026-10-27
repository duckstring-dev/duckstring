#!/bin/bash
# SF1000 StarRocks on four r7gd.2xlarge (together the r7gd.8xlarge's 32 vCPU and 256 GB), one run per scenario, on
# the split-file snapshots, checked against the 1 TB shakedown's DuckDB rebuilds.
export SF=1000 WORKERS=3 CHUNKED=1 REPS=1 REF_RUN=20261024T060655Z-sf1000_shakedown
bash aws/jobs/sr_cluster.sh
