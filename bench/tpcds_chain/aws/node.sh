#!/bin/bash
# EC2 user data for one benchmark node (launch.sh fills in the __PLACEHOLDERS__).
#
# Every node: formats the instance's NVMe at /mnt/nvme (spill, shuffle, a Catchment's state), installs
# Python, Duckstring and Trickle from the run's wheels, Java and Spark from the bucket's Spark kit, and the
# benchmark's code. Then by role:
#
#   single  runs jobs/__JOB__.sh
#   master  starts a Spark master and a worker, publishes its address, runs jobs/__JOB__.sh as the driver
#   worker  waits for the master's address, runs a Spark worker until the run is done
#
# A job with its own cluster (a jobs/{job}_worker.sh exists, as for StarRocks) skips Spark: the master just
# runs the job, and each worker runs jobs/{job}_worker.sh, which blocks until the run is done.
#
# Everything in /root/out goes to s3://__BUCKET__/results/__RUN__/{instance id}/ at the end, and the node
# powers off, which terminates it (launch.sh sets that). A hard timeout powers it off regardless.
set -uxo pipefail
export HOME=/root AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
BUCKET=__BUCKET__
RUN=__RUN__
ROLE=__ROLE__
JOB=__JOB__
CODE=__CODE__
TIMEOUT_MIN=__TIMEOUT_MIN__
RESULTS=s3://$BUCKET/results/$RUN
mkdir -p /root/out
exec > >(tee /root/out/node.log) 2>&1
IID=$(TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300") &&
      curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
TYPE=$(TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300") &&
       curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-type)

finish() {
  echo "=== finish $(date -u +%T)"
  free -g > /root/out/free.txt 2>&1; df -h > /root/out/df.txt 2>&1
  aws s3 cp --quiet --recursive /root/out "$RESULTS/$ROLE-$IID/"
  [ "$ROLE" = master ] && aws s3 cp --quiet - "$RESULTS/done" <<< "$(date -u +%FT%TZ)"
  shutdown -h now
}
trap finish EXIT
(sleep $((TIMEOUT_MIN * 60)); echo "HARD TIMEOUT"; aws s3 cp --recursive /root/out "$RESULTS/$ROLE-$IID-timeout/"; shutdown -h now) &
# Progress: the log and results so far, every two minutes, so a run can be followed from outside.
(while sleep 120; do aws s3 sync --quiet /root/out "$RESULTS/$ROLE-$IID/live/"; done) &

# --- The NVMe instance store, for spill, shuffle and a Catchment's state.
DEV=$(lsblk -dno NAME,MODEL | awk '/Instance Storage/ {print "/dev/"$1; exit}')
mkfs.xfs -f "$DEV" && mkdir -p /mnt/nvme && mount -o noatime "$DEV" /mnt/nvme
mkdir -p /mnt/nvme/tmp && chmod 1777 /mnt/nvme/tmp
export TMPDIR=/mnt/nvme/tmp

# --- Python, Duckstring and Trickle from this run's wheels, and the benchmark's code.
dnf install -y -q java-17-amazon-corretto-headless tar gzip
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH=/root/.local/bin:$PATH UV_CACHE_DIR=/mnt/nvme/uv-cache
mkdir -p /opt/bench && aws s3 cp --quiet --recursive "$CODE/" /opt/run/ && tar -xzf /opt/run/bench.tgz -C /opt/bench
uv venv -q -p 3.12 /opt/ds
uv pip install -q --python /opt/ds/bin/python /opt/run/wheels/duckstring_trickle-*.whl \
  "$(ls /opt/run/wheels/duckstring-*.whl)[aws]" psutil pymysql pyspark==4.2.0
export PATH=/opt/ds/bin:$PATH

# --- Spark 4.2 with S3A: hadoop-aws 3.5.0 and what it needs (the AWS SDK bundle and Amazon's S3 analytics
# accelerator, at the versions in Hadoop 3.5.0's build), and spark-hadoop-cloud for the magic committer.
# Assembled once and kept in the bucket as the Spark kit, so later nodes copy it in-region.
KIT=s3://$BUCKET/kit/spark-4.2.0-s3a-2.tgz
if ! aws s3 cp --quiet "$KIT" /mnt/nvme/kit.tgz; then
  curl -sSfL -o /mnt/nvme/spark.tgz https://archive.apache.org/dist/spark/spark-4.2.0/spark-4.2.0-bin-hadoop3.tgz
  tar -xzf /mnt/nvme/spark.tgz -C /mnt/nvme && mv /mnt/nvme/spark-4.2.0-bin-hadoop3 /mnt/nvme/spark
  M=https://repo1.maven.org/maven2
  for jar in org/apache/hadoop/hadoop-aws/3.5.0/hadoop-aws-3.5.0.jar \
             software/amazon/awssdk/bundle/2.35.4/bundle-2.35.4.jar \
             software/amazon/s3/analyticsaccelerator/analyticsaccelerator-s3/1.3.1/analyticsaccelerator-s3-1.3.1.jar \
             org/apache/spark/spark-hadoop-cloud_2.13/4.2.0/spark-hadoop-cloud_2.13-4.2.0.jar; do
    curl -sSfL -o "/mnt/nvme/spark/jars/$(basename $jar)" "$M/$jar"
  done
  tar -czf /mnt/nvme/kit.tgz -C /mnt/nvme spark && aws s3 cp --quiet /mnt/nvme/kit.tgz "$KIT"
else
  tar -xzf /mnt/nvme/kit.tgz -C /mnt/nvme
fi
export SPARK_HOME=/mnt/nvme/spark JAVA_HOME=/usr/lib/jvm/java-17-amazon-corretto
export SPARK_LOCAL_DIRS=/mnt/nvme/spark-local SPARK_WORKER_DIR=/mnt/nvme/spark-work
mkdir -p "$SPARK_LOCAL_DIRS" "$SPARK_WORKER_DIR"
echo "{\"instance\": \"$IID\", \"type\": \"$TYPE\", \"role\": \"$ROLE\", \"code\": \"$CODE\"," \
     "\"cpus\": $(nproc), \"mem_gb\": $(free -g | awk '/Mem:/ {print $2}')}" > /root/out/node.json
echo "=== ready $(date -u +%T)"

cd /opt/bench
export BUCKET RUN RESULTS
nohup python aws/sampler.py /root/out/samples.csv > /dev/null 2>&1 &  # CPU, network, disk each second
case "$ROLE" in
  single)
    bash "aws/jobs/$JOB.sh"
    ;;
  master)
    if [ -f "aws/jobs/${JOB}_worker.sh" ]; then bash "aws/jobs/$JOB.sh"; exit; fi
    IP=$(hostname -I | awk '{print $1}')
    SPARK_MASTER_HOST=$IP "$SPARK_HOME/sbin/start-master.sh"
    "$SPARK_HOME/sbin/start-worker.sh" "spark://$IP:7077"
    aws s3 cp --quiet - "$RESULTS/master" <<< "$IP"
    export SPARK_MASTER="spark://$IP:7077"
    bash "aws/jobs/$JOB.sh"
    ;;
  worker)
    if [ -f "aws/jobs/${JOB}_worker.sh" ]; then bash "aws/jobs/${JOB}_worker.sh"; exit; fi
    until IP=$(aws s3 cp --quiet "$RESULTS/master" - 2>/dev/null); do sleep 5; done
    "$SPARK_HOME/sbin/start-worker.sh" "spark://$IP:7077"
    until aws s3 ls "$RESULTS/done" >/dev/null 2>&1; do sleep 15; done
    ;;
esac
