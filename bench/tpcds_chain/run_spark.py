"""The Spark baseline: rebuild the whole chain from a snapshot with Spark SQL, as one job.

    python run_spark.py --data LOC --version 1 --out LOC [--master local[*]] [--driver-memory 24g]
                        [--executor-memory 40g] [--executor-cores 8] [--scratch DIR] [--conf KEY=VALUE ...]

Runs the queries in chain_sql.py, writing the enriched sales and the three aggregates as Parquet under --out,
and prints a JSON timing line. The time starts before PySpark is imported, so it includes starting the JVM
and the Spark session (`startup`), the part of Spark's overhead a user waits through on every job. It doesn't
include provisioning a cluster.

--data and --out may be local or s3://, read and written through S3A (the hadoop-aws and AWS SDK jars must
be on Spark's classpath; aws/node.sh puts them there). Output to S3 uses the S3A magic committer, so tasks
write their files in place: the default committer renames each file at commit, which on S3 is a full copy.
That needs the spark-hadoop-cloud jar too. --master spark://HOST:7077 runs on a standalone cluster; Spark
spills and shuffles to --scratch (the driver's, and the executors' in local mode).
"""

from __future__ import annotations

import time

T0 = time.perf_counter()

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from chain_sql import AGGREGATES, ENRICHED, SOURCES  # noqa: E402

S3A_COMMITTER = {
    "spark.hadoop.fs.s3a.committer.name": "magic",
    "spark.hadoop.fs.s3a.committer.magic.enabled": "true",
    "spark.sql.sources.commitProtocolClass": "org.apache.spark.internal.io.cloud.PathOutputCommitProtocol",
    "spark.sql.parquet.output.committer.class": "org.apache.spark.internal.io.cloud.BindingParquetOutputCommitter",
}


def _loc(loc: str) -> str:
    """A location as Spark reads it: s3:// becomes S3A, a local path becomes absolute."""
    if loc.startswith("s3://"):
        return "s3a://" + loc.removeprefix("s3://")
    return loc if "://" in loc else str(Path(loc).resolve())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--version", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--master", default="local[*]")
    ap.add_argument("--driver-memory", default="24g")
    ap.add_argument("--executor-memory")
    ap.add_argument("--executor-cores")
    ap.add_argument("--scratch", help="a local directory for shuffle and spill files")
    ap.add_argument("--conf", action="append", default=[], help="an extra Spark setting, KEY=VALUE")
    args = ap.parse_args()
    data, out = _loc(args.data), _loc(args.out)
    remote = "://" in out
    if not remote:
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
    scratch = args.scratch or (tempfile.mkdtemp(prefix="spark_scratch_") if remote else f"{out}/_tmp")

    from pyspark.sql import SparkSession
    from pyspark.sql.utils import AnalysisException

    builder = (
        SparkSession.builder.master(args.master)
        .appName("tpcds_chain")
        .config("spark.driver.memory", args.driver_memory)
        .config("spark.local.dir", scratch)
        .config("spark.ui.enabled", "false")
        .config("spark.ui.showConsoleProgress", "false")
    )
    if args.executor_memory:
        builder = builder.config("spark.executor.memory", args.executor_memory)
    if args.executor_cores:
        builder = builder.config("spark.executor.cores", args.executor_cores)
    if "s3a://" in data + out:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        builder = builder.config("spark.hadoop.fs.s3a.endpoint.region", region)
    if remote:
        for k, v in S3A_COMMITTER.items():
            builder = builder.config(k, v)
    for kv in args.conf:
        k, _, v = kv.partition("=")
        builder = builder.config(k, v)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    startup = time.perf_counter() - T0

    snap = f"{data}/snapshot/v{args.version}"
    for table in SOURCES:
        try:  # one file, or a directory of chunks for a large table
            df = spark.read.parquet(f"{snap}/{table}.parquet")
        except AnalysisException:
            df = spark.read.parquet(f"{snap}/{table}")
        df.createOrReplaceTempView(table)
    stages = {}
    t = time.perf_counter()
    spark.sql(ENRICHED).write.mode("overwrite").parquet(f"{out}/enriched")
    stages["enriched"] = time.perf_counter() - t
    spark.read.parquet(f"{out}/enriched").createOrReplaceTempView("enriched")
    for name, sql in AGGREGATES.items():
        t = time.perf_counter()
        spark.sql(sql).write.mode("overwrite").parquet(f"{out}/{name}")
        stages[name] = time.perf_counter() - t
    version = spark.version
    spark.stop()
    total = time.perf_counter() - T0
    shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps({"engine": "spark", "spark": version, "master": args.master, "version": args.version,
                      "seconds": round(total, 2), "startup": round(startup, 2),
                      "stages": {k: round(v, 2) for k, v in stages.items()}}))


if __name__ == "__main__":
    main()
