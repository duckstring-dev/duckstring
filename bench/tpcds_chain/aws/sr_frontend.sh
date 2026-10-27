#!/bin/bash
# A StarRocks frontend and backend on this (the master) node, then every worker's backend registered
# (aws/sr_backend.sh publishes their addresses), waiting until all WORKERS + 1 backends are alive.
# The all-in-one image can't be used across machines: its frontend calls itself 127.0.0.1, and a backend on
# another machine refuses its heartbeats.
set -euxo pipefail
IP=$(hostname -I | awk '{print $1}')
FE_IMAGE=${STARROCKS_FE_IMAGE:-starrocks/fe-ubuntu:4.1.6}
BE_IMAGE=${STARROCKS_BE_IMAGE:-starrocks/be-ubuntu:4.1.6}
bash aws/docker_up.sh
docker pull -q "$FE_IMAGE"; docker pull -q "$BE_IMAGE"
docker run -d --name starrocks-fe --network host "$FE_IMAGE" bash -c \
  "echo 'priority_networks = $IP/32' >> /opt/starrocks/fe/conf/fe.conf && /opt/starrocks/fe/bin/start_fe.sh"
# This node's backend shares the machine with the frontend's JVM, so it takes 70% of memory, not the default 90%.
docker run -d --name starrocks-be --network host "$BE_IMAGE" bash -c \
  "printf 'priority_networks = $IP/32\nmem_limit = 70%%\n' >> /opt/starrocks/be/conf/be.conf && /opt/starrocks/be/bin/start_be.sh"
until python -c "import pymysql; pymysql.connect(host='127.0.0.1', port=9030, user='root', connect_timeout=3)" 2>/dev/null; do
  sleep 5
done
until [ "$(aws s3 ls "$RESULTS/workers/" 2>/dev/null | wc -l)" -ge "$WORKERS" ]; do sleep 5; done
for ip in "$IP" $(aws s3 ls "$RESULTS/workers/" | awk '{print $4}'); do
  python -c "import pymysql; pymysql.connect(host='127.0.0.1', port=9030, user='root', autocommit=True).cursor().execute(\"ALTER SYSTEM ADD BACKEND '$ip:9050'\")"
done
until python - <<PY
import pymysql
cur = pymysql.connect(host="127.0.0.1", port=9030, user="root", connect_timeout=3).cursor()
cur.execute("SHOW BACKENDS")
cols = [d[0] for d in cur.description]
rows = [dict(zip(cols, r)) for r in cur.fetchall()]
print(sum(r.get("Alive") == "true" for r in rows), "backends alive", [r.get("ErrMsg") for r in rows if r.get("ErrMsg")])
assert sum(r.get("Alive") == "true" for r in rows) >= $WORKERS + 1
PY
do sleep 5; done
python -c "
import pymysql, json
cur = pymysql.connect(host='127.0.0.1', port=9030, user='root').cursor(); cur.execute('SHOW BACKENDS')
cols = [d[0] for d in cur.description]
print(json.dumps([{k: str(v) for k, v in zip(cols, r)} for r in cur.fetchall()], indent=1))" > /root/out/backends.json
