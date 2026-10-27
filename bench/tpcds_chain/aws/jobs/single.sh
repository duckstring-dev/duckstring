#!/bin/bash
# The single-machine runs: for each scenario and repeat, DuckDB's rebuild, Duckstring's first build and change,
# and Spark's rebuild in local mode, each wrapped in resources.py (memory, spill, disk), each output checked
# against that scenario's first DuckDB rebuild. Inputs from s3://$BUCKET/data/sf{SF}_{scenario}/, outputs to
# s3://$BUCKET/runs/$RUN/. Set by the job that sources this: SF, and optionally SCENARIOS, REPS, ENGINES, SPARK_MEM,
# REF_RUN (check against that run's first DuckDB rebuild instead of this run's), and DATA_SUFFIX (read
# data/sf{SF}_{scenario}{suffix}, e.g. "chunked" for chunk_copy.py's split snapshots), VERSION (the snapshot
# DuckDB rebuilds and the outputs are checked against: default 1, the state after one batch), and DS_ARGS (more
# run_duckstring.py arguments, e.g. "--continuous 7 --every 10").
#
# Duckstring engines: `duckstring` runs with the Catchment's defaults (memory slots on, N=4); `duckstring_off`
# with slots off (DUCKSTRING_RUN_SLOTS=0, the behaviour before them); `duckstring_n1`, `_n2`, ... with N slots;
# `duckstring_prof` with the defaults and the step profiler (prof/sitecustomize.py), which costs time, so it is
# kept apart from the timed runs. Every Duckstring run records each Duck's memory (DS_MEM_LOG), the Catchment's
# status every 0.25 s (status_{tag}.jsonl) and its whole run history (in runs_{tag}.json), and every run's
# machine-wide memory and CPU by process (res_{tag}.json). Kernel OOM kills are appended to oom.txt.
# The starrocks engine needs StarRocks running here (aws/starrocks_up.sh); its server's peak memory (the
# container's cgroup) is recorded after each run.
set -uxo pipefail
SCENARIOS=${SCENARIOS:-mix fact append}
REPS=${REPS:-3}
ENGINES=${ENGINES:-duckdb duckstring spark}
SPARK_MEM=${SPARK_MEM:-96g}
OUT=s3://$BUCKET/runs/$RUN
export DUCKSTRING_RELAY=off
VERSION=${VERSION:-1}
export PYTHONPATH="$PWD/prof${PYTHONPATH:+:$PYTHONPATH}"
R() { python resources.py --interval 0.25 "$@"; }
oom() {  # the kernel's OOM kills since the last call, labelled with the run
  dmesg -T 2>/dev/null | grep -iE "out of memory|oom-kill|killed process" > /root/out/.dmesg_now
  comm -13 /root/out/.dmesg_seen /root/out/.dmesg_now 2>/dev/null | sed "s/^/$1 /" >> /root/out/oom.txt
  mv /root/out/.dmesg_now /root/out/.dmesg_seen
}
touch /root/out/.dmesg_seen /root/out/oom.txt
for sc in $SCENARIOS; do
  DATA=s3://$BUCKET/data/sf${SF}_$sc${DATA_SUFFIX:-}
  REF=$OUT/duckdb_${sc}_1
  [ -n "${REF_RUN:-}" ] && REF=s3://$BUCKET/runs/$REF_RUN/duckdb_${sc}_1
  for rep in $(seq 1 "$REPS"); do
    for engine in $ENGINES; do
      tag=${engine}_${sc}_$rep rc=
      echo "=== $(date -u +%T) $tag"
      case "$engine" in
        duckdb)
          R --out "/root/out/res_$tag.json" --spill /mnt/nvme/spill -- \
            python run_duckdb.py --data "$DATA" --version "$VERSION" --out "$OUT/$tag" --scratch /mnt/nvme/spill \
            > "/root/out/$tag.json"; rc=$? ;;
        duckstring*)
          (
            case "$engine" in
              duckstring_off) export DUCKSTRING_RUN_SLOTS=0 ;;
              duckstring_n*) export DUCKSTRING_RUN_SLOTS=${engine#duckstring_n} ;;
              duckstring_prof) export DS_PROF_LOG="/root/out/prof_$tag" ;;
            esac
            export DS_MEM_LOG="/root/out/mem_$tag"
            # shellcheck disable=SC2086
            R --out "/root/out/res_$tag.json" --spill "/mnt/nvme/ds/tmp" --spill "/mnt/nvme/ds/ponds/*/m1/*.tmp" -- \
              python run_duckstring.py --data "$DATA" --root /mnt/nvme/ds --data-root "$OUT/$tag" \
              --status-log "/root/out/status_$tag.jsonl" --results "/root/out/runs_$tag.json" ${DS_ARGS:-} \
              > "/root/out/$tag.json"
          ); rc=$?
          cp /mnt/nvme/ds.log "/root/out/catchment_$tag.log" 2>/dev/null
          rm -rf /mnt/nvme/ds ;;
        spark)
          R --out "/root/out/res_$tag.json" -- \
            python run_spark.py --data "$DATA" --version 1 --out "$OUT/$tag" --driver-memory "$SPARK_MEM" \
            --scratch /mnt/nvme/spark-local > "/root/out/$tag.json"; rc=$? ;;
        starrocks|starrockscluster)
          R --out "/root/out/res_$tag.json" -- \
            python run_starrocks.py --data "$DATA" --version 1 --out "$OUT/$tag" > "/root/out/$tag.json"; rc=$?
          CG=$(find /sys/fs/cgroup -type d -name "docker-$(docker inspect -f '{{.Id}}' starrocks)*" | head -1)
          echo "$tag memory.peak $(cat "$CG/memory.peak" 2>/dev/null)" >> /root/out/starrocks_memory.txt ;;
      esac
      echo "exit $rc $tag" >> /root/out/exits.txt
      oom "$tag"
      if [ "$tag" != "duckdb_${sc}_1" ] || [ -n "${REF_RUN:-}" ]; then
        target=$OUT/$tag; case "$engine" in duckstring*) target=duckstring:$OUT/$tag ;; esac
        python verify.py "$target" "$REF" > "/root/out/verify_$tag.txt" 2>&1
        echo "verify $? $tag" >> /root/out/exits.txt
      fi
      [ "$rep" != 1 ] && aws s3 rm --quiet --recursive "$OUT/$tag"  # keep the first repeat's outputs only
    done
  done
done
