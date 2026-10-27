#!/bin/bash
# A StarRocks backend on a worker node: start it and publish this node's address for the frontend to register. Each StarRocks process is told this node's address
# (priority_networks); host networking, so the backend reaches S3 with the instance's role.
set -euxo pipefail
IMAGE=${STARROCKS_BE_IMAGE:-starrocks/be-ubuntu:4.1.6}
IP=$(hostname -I | awk '{print $1}')
bash aws/docker_up.sh
docker pull -q "$IMAGE"
docker run -d --name starrocks-be --network host "$IMAGE" bash -c \
  "echo 'priority_networks = $IP/32' >> /opt/starrocks/be/conf/be.conf && /opt/starrocks/be/bin/start_be.sh"
aws s3 cp --quiet - "$RESULTS/workers/$IP" <<< "$IP"
