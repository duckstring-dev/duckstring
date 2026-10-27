"""The most this machine can read from S3: parallel ranged GETs over a prefix's objects, the bytes thrown away.

    python aws/s3_ceiling.py s3://bucket/prefix [--threads 64] [--seconds 20] [--start EPOCH]

Prints JSON: bytes read, the average rate, and the best one-second rate. --start waits until that wall-clock time,
so every node of a cluster can read at once (their rates add up to the cluster's). Reads 16 MB ranges, cycling
through the objects, from a pool of threads sharing one client.
"""

import argparse
import itertools
import json
import threading
import time

import boto3
from botocore.config import Config

CHUNK = 16 * 1024 * 1024


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix")
    ap.add_argument("--threads", type=int, default=64)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--start", type=float)
    args = ap.parse_args()
    bucket, _, prefix = args.prefix.removeprefix("s3://").partition("/")
    s3 = boto3.client("s3", config=Config(max_pool_connections=args.threads + 8, retries={"max_attempts": 5}))
    objs = [(o["Key"], o["Size"]) for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", []) if o["Size"] > 0]
    ranges = itertools.cycle([(k, off, min(off + CHUNK, size) - 1) for k, size in objs for off in range(0, size, CHUNK)])
    lock, per_second, total = threading.Lock(), {}, [0]
    if args.start:
        time.sleep(max(0.0, args.start - time.time()))
    t0 = time.time()
    end = t0 + args.seconds

    def worker():
        while time.time() < end:
            with lock:
                key, a, b = next(ranges)
            body = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={a}-{b}")["Body"]
            while chunk := body.read(1024 * 1024):
                sec = int(time.time() - t0)
                with lock:
                    total[0] += len(chunk)
                    per_second[sec] = per_second.get(sec, 0) + len(chunk)

    threads = [threading.Thread(target=worker) for _ in range(args.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    window = sum(v for s, v in per_second.items() if s < int(args.seconds))  # requests finishing late don't count
    full = [v for s, v in per_second.items() if 0 < s < int(args.seconds) - 1]  # whole seconds, not the ramp
    print(json.dumps({"threads": args.threads, "seconds": args.seconds, "bytes": window,
                      "avg_gbit_s": round(window * 8 / int(args.seconds) / 1e9, 2),
                      "best_second_gbit_s": round(max(full) * 8 / 1e9, 2) if full else None,
                      "start": t0}))


if __name__ == "__main__":
    main()
