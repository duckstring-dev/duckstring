#!/bin/bash
# Docker, with its storage on the NVMe (so StarRocks' storage and spill are too).
set -euxo pipefail
dnf install -y -q docker
mkdir -p /etc/docker /mnt/nvme/docker
echo '{"data-root": "/mnt/nvme/docker"}' > /etc/docker/daemon.json
systemctl start docker
