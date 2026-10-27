#!/bin/bash
# SF100 on one machine (m7gd.8xlarge): see single.sh.
export SF=100
bash aws/jobs/single.sh
aws s3 cp --quiet - "s3://$BUCKET/runs/ref_sf100" <<< "$RUN"  # the reference outputs for the cluster job
