#!/bin/bash
# A StarRocks backend on a worker node (aws/sr_backend.sh), kept running until the run is done.
set -euxo pipefail
bash aws/sr_backend.sh
until aws s3 ls "$RESULTS/done" >/dev/null 2>&1; do sleep 15; done
docker logs --tail 200 starrocks-be > /root/out/be.log 2>&1 || true
