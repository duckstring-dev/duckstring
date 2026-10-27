#!/bin/bash
# SF1000 again on one r7gd.8xlarge after plans/trickle-insert-only.md and plans/memory-budget.md, with six memory
# slots (one per Pond, so no Pond waits for one). Builds the data from the public text (the bucket was emptied),
# then:
#
#   1. the three scenarios once each: DuckDB, and Duckstring with six slots;
#   2. new sales with slots off (the scenario that peaked at 144 GB unconstrained), for the memory comparison;
#   3. one profiled Duckstring run of new sales (six slots), kept out of the timings.
set -uxo pipefail
export SF=1000 SRC=s3://redshift-downloads/TPC-DS/2.13/1TB DUCKSTRING_RUN_SLOTS=6
bash aws/jobs/prep.sh
SCENARIOS="append fact mix" ENGINES="duckdb duckstring" REPS=1 bash aws/jobs/single.sh
SCENARIOS=append ENGINES="duckstring_off duckstring_prof" REPS=1 bash aws/jobs/single.sh
