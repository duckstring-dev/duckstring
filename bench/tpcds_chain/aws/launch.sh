#!/bin/bash
# Launch benchmark nodes in us-east-1 (the region of the public TPC-DS text). From a laptop:
#
#     aws/launch.sh --type m7gd.8xlarge --job sf100_single --code s3://…/code/abc1234 [--count 4] [--timeout 180]
#
# --count 1 launches one `single` node; more launches a Spark master plus workers. Each node runs aws/node.sh,
# writes its results to s3://$BUCKET/results/{run}/ and terminates itself. Prints the run id and instance ids.
# Graviton (arm64) types with local NVMe (the *gd family) are assumed: the AMI is AL2023 for arm64.
set -euo pipefail
export AWS_REGION=us-east-1
BUCKET=${BUCKET:?set BUCKET to your results bucket}
SG=${SG:?set SG to a security group whose members reach each other, with nothing inbound}
PROFILE=${PROFILE:-duckstring-catchment}   # instance role, granted the bucket by its bucket policy
SUBNET=${SUBNET:-}                          # default: the default VPC's subnet in us-east-1a
TYPE= JOB= CODE= COUNT=1 TIMEOUT=180
while [ $# -gt 0 ]; do
  case "$1" in
    --type) TYPE=$2; shift 2 ;;
    --job) JOB=$2; shift 2 ;;
    --code) CODE=$2; shift 2 ;;
    --count) COUNT=$2; shift 2 ;;
    --timeout) TIMEOUT=$2; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$TYPE" ] && [ -n "$JOB" ] && [ -n "$CODE" ] || { echo "need --type, --job and --code" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/jobs/$JOB.sh" ] || { echo "no job $JOB" >&2; exit 2; }
RUN=$(date -u +%Y%m%dT%H%M%SZ)-$JOB
SUBNET=${SUBNET:-$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true Name=availability-zone,Values=us-east-1a \
  --query 'Subnets[0].SubnetId' --output text)}
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query Parameter.Value --output text)

userdata() {
  sed -e "s|__BUCKET__|$BUCKET|; s|__RUN__|$RUN|; s|__ROLE__|$1|; s|__JOB__|$JOB|; s|__CODE__|$CODE|" \
      -e "s|__TIMEOUT_MIN__|$TIMEOUT|" "$HERE/node.sh"
}

launch() {  # role, count
  local ud; ud=$(mktemp); userdata "$1" > "$ud"
  aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" --count "$2" --subnet-id "$SUBNET" \
    --security-group-ids "$SG" --iam-instance-profile "Name=$PROFILE" --associate-public-ip-address \
    --instance-initiated-shutdown-behavior terminate --metadata-options HttpTokens=required,HttpEndpoint=enabled \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=40,VolumeType=gp3,DeleteOnTermination=true}' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=bench-$1},{Key=project,Value=duckstring-bench},{Key=run,Value=$RUN}]" \
    --user-data "file://$ud" --query 'Instances[].InstanceId' --output text
  rm -f "$ud"
}

echo "run $RUN"
echo "results s3://$BUCKET/results/$RUN/"
if [ "$COUNT" -eq 1 ]; then
  launch single 1
else
  launch master 1
  launch worker $((COUNT - 1))
fi
