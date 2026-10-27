#!/bin/bash
# SF1000 Spark on four r7gd.2xlarge (8 vCPU, 64 GB each: together the r7gd.8xlarge's 32 vCPU and 256 GB), one run
# per scenario on the split-file snapshots StarRocks read, checked against the 1 TB shakedown's DuckDB rebuilds.
export SF=1000 WORKERS=4 EXEC_MEM=40g EXEC_CORES=8 DRIVER_MEM=8g REPS=1 DATA_SUFFIX=chunked
export REF_RUN=20261024T060655Z-sf1000_shakedown
bash aws/jobs/cluster.sh
