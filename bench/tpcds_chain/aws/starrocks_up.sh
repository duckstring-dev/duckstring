#!/bin/bash
# Start StarRocks all-in-one (a frontend and a backend) on this node and wait until its backend is alive.
# Docker keeps its storage on the NVMe, so StarRocks' spill does too. Host networking lets the backend reach
# S3 with the instance's role (its metadata service is a hop too far from a bridged container).
set -euxo pipefail
IMAGE=${STARROCKS_IMAGE:-starrocks/allin1-ubuntu:4.1.6}
bash aws/docker_up.sh
docker pull -q "$IMAGE"
docker run -d --name starrocks --network host "$IMAGE"
until python - <<'PY'
import pymysql
cur = pymysql.connect(host="127.0.0.1", port=9030, user="root", connect_timeout=3).cursor()
cur.execute("SHOW BACKENDS")
cols = [d[0] for d in cur.description]
assert any(dict(zip(cols, r)).get("Alive") == "true" for r in cur.fetchall())
PY
do sleep 5; done
docker exec starrocks bash -c 'ls -d /data/deploy/starrocks/* 2>/dev/null; nproc; free -g' > /root/out/starrocks.txt 2>&1 || true
