#!/bin/bash
# Build the data from the public TPC-DS text (SF100 unless SF and SRC say otherwise): the three scenarios'
# change and snapshot, generated on the NVMe and uploaded to s3://$BUCKET/data/sf{SF}_{scenario}/.
# Run on a single node (node.sh sets BUCKET).
set -euxo pipefail
SF=${SF:-100}
SRC=${SRC:-s3://redshift-downloads/TPC-DS/2.13/100GB}
D=/mnt/nvme/data
python gen.py --sf "$SF" --data "$D/sf${SF}_mix" --scenario mix --source "$SRC" 2>&1 | tee /root/out/gen_mix.log
for sc in fact append; do
  python gen.py --sf "$SF" --data "$D/sf${SF}_$sc" --scenario "$sc" --base-from "$D/sf${SF}_mix" 2>&1 | tee "/root/out/gen_$sc.log"
done
for sc in mix fact append; do
  cp "$D/sf${SF}_$sc/manifest.json" "/root/out/manifest_$sc.json"
  # snapshot/v0 is the base again, which no runner reads (the baselines rebuild from v1).
  aws s3 sync --quiet --exclude "landing/RELEASED" --exclude "_tmp/*" --exclude "snapshot/v0/*" \
    "$D/sf${SF}_$sc" "s3://$BUCKET/data/sf${SF}_$sc/"
done
aws s3 ls --recursive --summarize "s3://$BUCKET/data/" | tail -2 > /root/out/data_size.txt
