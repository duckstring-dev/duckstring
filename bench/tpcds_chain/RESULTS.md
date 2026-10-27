# TPC-DS chain benchmark: results

Results as of 2026-10-27, from the harness in this directory. Every output of every run was checked against
DuckDB's rebuild of the same snapshot (verify.py: aggregates row by row to one part in 10^9, the enriched table by
row count and a sum of row hashes), and every one matched. Run ids are directories in `results/aws/`, which holds
each run's timings, resource traces, checks and node logs (the bucket has been emptied).

## What was measured

The pipeline is TPC-DS's store sales, enriched with five dimensions and its returns, then aggregated three ways
(see README.md). Each engine runs the whole pipeline; Duckstring also absorbs a change after its first build.
Three changes, each touching about 0.5% of the enriched sales:

| Change | SF100 | SF1000 |
|---|---|---|
| Append only | 1,344,948 new sales | 13,430,179 new sales |
| Fact only | 538,735 new, 130,336 corrected, 47,837 voided sales; 72,665 new returns | 5,371,081 new, 1,300,729 corrected, 483,725 voided; 724,749 returns |
| Mixed | the fact change, plus 97 items reclassified and 1,974 customers moved | plus 143 items and 11,757 customers |

The fact table is 268.6M rows at SF100 and 2.69 billion at SF1000 (TPC-DS store sales with a sale date and store).
As Parquet, keeping only the columns the pipeline reads, a snapshot is 4.7 GB at SF100 and 47 GB at SF1000.

The engines:

- **DuckDB 2.0**: the pipeline as one SQL script (run_duckdb.py).
- **Duckstring 0.7.1** (on DuckDB 2.0): six Ponds on a Catchment with one Duck per Pond, with memory slots
  dividing the machine between them (four at SF100, six at SF1000, so no Pond waited for one). A phase is timed from the Refresh until every Pond has finished ("finished", its output published
  on the machine) and until every Pond's copy on S3 has caught up ("on S3").
- **Spark 4.2**: the same SQL, local mode on one machine or a standalone cluster (run_spark.py). Startup of the
  JVM and session is included (about 5 s on EC2); provisioning isn't.
- **StarRocks 4.1.6**: the same SQL, reading with FILES() and writing with INSERT INTO FILES
  (run_starrocks.py). All-in-one container on one machine; one frontend and four backends on a cluster.

Every engine runs as it ships: no tuning, no caches carried between runs. On AWS every run reads its inputs
from S3 and writes its outputs to S3 (us-east-1), the way a cloud batch job runs and then turns off. Clusters
have the same total cores and memory as the single machine they're compared with.

## Laptop, SF100 (MacBook Air M4, 10 cores, 32 GB, local SSD)

Each run after at least two minutes idle (the fanless laptop throttles under sustained load: DuckDB's rebuild
took 45 s warm against 25 s cool). One run each.

| Change | DuckDB rebuild | Spark rebuild | Duckstring first build | Duckstring change |
|---|---|---|---|---|
| Append only | 26.0 s | | 39.8 s | **2.0 s** |
| Fact only | 25.2 s | | 34.2 s | **4.3 s** |
| Mixed | 25.2 s | 200 s | 33.7 s | **7.4 s** |

StarRocks 4.1.6 on the same laptop (mixed snapshot): **62.0 s** median of three (58.6 to 64.7), peaking at 14.9 GB,
verified. It only runs on Linux, so it ran in its all-in-one container in a Colima VM given all 10 cores and 24 GB,
reading a copy of the snapshot inside the container (results/laptop_starrocks_2026-10-25.json); DuckDB and Spark
ran natively.

Peak memory (resources.py): DuckDB 2.6 GB, Spark 12.3 GB (24 GB heap). Nothing spilled or swapped.

## AWS, SF100

One m7gd.8xlarge (32 vCPU, 128 GB, NVMe) for the single-machine runs; four m7gd.2xlarge (8 vCPU, 32 GB each)
for the clusters. Median of three runs (range in brackets).

| Change | DuckDB | Spark, 1 node | Spark, 4 nodes | StarRocks, 1 node | StarRocks, 4 nodes | Duckstring first build: finished / on S3 | Duckstring change: finished / on S3 |
|---|---|---|---|---|---|---|---|
| Append only | 16.9 s (16.8-17.1) | 111 s | 124 s | 65.6 s | 58.5 s | 24.2 / 40.9 s | **4.0** / 5.0 s |
| Fact only | 17.2 s (16.7-17.4) | 111 s | 122 s | 66.4 s | 56.3 s | 19.3 / 35.4 s | **5.4** / 6.2 s |
| Mixed | 17.6 s (17.1-17.7) | 111 s | 122 s | 64.3 s | 60.0 s | 19.7 / 36.3 s | **7.7** / 8.5 s |

Peak memory: DuckDB 12 GB, Duckstring 7.5 to 11.7 GB over a whole run (first build and change) and 4.2 to 5.8 GB
while absorbing a change, Spark 56 to 57 GB, StarRocks about 21.5 GB. Nothing spilled or swapped. Runs:
20261024T051820Z-sf100_single (DuckDB, Spark), 20261026T215026Z-sf100_rerun (Duckstring, three runs each; its
DuckDB rebuilds were within 5% of the first run's), 20261024T045309Z-sf100_cluster, 20261024T084008Z-sf100_starrocks,
20261024T094159Z-sf100_srcluster.

## AWS, SF1000

One r7gd.8xlarge (32 vCPU, 256 GB, NVMe); four r7gd.2xlarge (8 vCPU, 64 GB each) for the clusters. One run each.
StarRocks, Spark's cluster and the second DuckDB column read a copy of the snapshots with the two fact tables
split into 256 MB files (chunk_copy.py); everything else read the single-file snapshots.

| Change | DuckDB | DuckDB, split files | StarRocks, 4 nodes, split files | Spark, 4 nodes, split files | Duckstring first build: finished / on S3 | Duckstring change: finished / on S3 |
|---|---|---|---|---|---|---|
| Append only | 166.4 s | 166.3 s | 592 s | 1,607 s | 210 / 398 s | **12.9** / 15.1 s |
| Fact only | 166.9 s | 165.1 s | 607 s | 1,590 s | 163 / 341 s | **23.5** / 25.6 s |
| Mixed | 166.9 s | 165.9 s | 622 s | 1,605 s | 162 / 337 s | **37.5** / 39.5 s |

Peak memory: DuckDB 109 to 113 GB; Duckstring 51 to 73 GB over a whole run and 14 to 24 GB while absorbing a
change, with nothing spilled on 256 GB (with memory slots off, new sales peaked at 140 GB in the same time). Runs:
20261024T060655Z-sf1000_shakedown (DuckDB), 20261026T235158Z-sf1000_rerun (Duckstring, and DuckDB again within
5%), 20261024T110817Z-sf1000_duckdb_chunked, 20261024T101026Z-sf1000_srcluster, 20261024T120205Z-sf1000_cluster.

## AWS, SF100: where the time goes on S3

Runs 20261025T013218Z-s3_single (one m7gd.8xlarge) and 20261025T014611Z-s3_cluster (four m7gd.2xlarge), the mixed
change, with every machine's CPU and network sampled once a second (aws/sampler.py, summarised by aws/s3_report.py).
On one machine all network traffic is S3. On a cluster, traffic between its machines leaves one and enters another, so
its total in minus total out is S3 reads minus S3 writes (the duckflock harness's method, which matched Trino's own
accounting to 3%); for both clusters it came within 0.1 GB of the same engine's single-machine run, so the clusters'
S3 volumes are taken as the single runs' and the rest of their inbound traffic is between machines. The peak S3 rate
for a cluster is the per-second in minus out (a lower bound while it writes). Rated bandwidth is AWS's
(describe-instance-types): m7gd.8xlarge 15 Gbit/s; m7gd.2xlarge 3.75 Gbit/s baseline, 15 peak.

| Run | Wall | Read from S3 | Written to S3 | Between machines | Peak S3 read | CPU busy |
|---|---|---|---|---|---|---|
| DuckDB, 1 machine, from S3 | 16.8 s | 8.4 GB | 4.9 GB | none | 13.3 Gbit/s | 60% |
| DuckDB, 1 machine, from local NVMe (page cache dropped) | 11.3 s | none | none | none | none | 80% |
| Spark, 1 machine | 112 s | 14.2 GB | 9.8 GB | none | 8.5 Gbit/s | 53% |
| Spark, 4 machines | 123 s | about 14 GB | about 10 GB | about 22 GB | 9.2 Gbit/s | 51% |
| StarRocks, 1 machine | 60 s | 10.2 GB | 5.3 GB | none | 3.5 Gbit/s | 28% |
| StarRocks, 4 machines | 57 s | about 10 GB | about 5 GB | about 1.6 GB | 3.3 Gbit/s | 37% |

- The single machine is rated 15 Gbit/s; four m7gd.2xlarge have the same 15 sustained (3.75 each) and burst to 60.
  DuckDB peaked at 13.3, about 90% of its rating. Neither cluster engine reached even 15, and neither read faster on
  four machines than on one. Spark's cluster also shuffled about 22 GB between its machines.
- `aws/s3_ceiling.py` (a Python boto3 downloader) is not a ceiling: it peaked at 9.2 Gbit/s on one machine and got
  slower with more threads (8.7 at 32, 7.0 at 128, CPU about 7%), the signature of Python's interpreter lock; DuckDB's
  native reads beat it. Four nodes together reached 29.4 Gbit/s in a 20 s burst.
- S3 cost DuckDB about a third of its time (16.8 s against 11.3 s). Its profiler: 1.7 s blocked across the four
  stages; the extra wall time is in the join (10.9 against 9.1 s) and the first aggregate, which reads the written
  enriched table back from S3 (2.8 against 0.5 s); the next two re-read only 0.06 and 0.22 GB from S3 (DuckDB's
  in-run cache) against about 2 GB each from local disk.
- StarRocks read slowly with its CPUs mostly idle, on one machine or four: bound by how it reads S3 as it ships.

## What the numbers say

- **One DuckDB machine was the fastest way to rebuild the pipeline.** On the same total hardware, a StarRocks
  cluster took about 3.4 times as long at SF100 and 3.6 times at SF1000; a Spark cluster about 7 times at SF100
  and 9.7 times at SF1000, where its join took 1,391 to 1,407 s of its 1,590 to 1,607.
- **Distribution didn't pay at these sizes.** Spark on four nodes was 10% slower than on one at SF100. StarRocks
  on four was 10% faster than on one at SF100, and its ratio to DuckDB stayed flat from SF100 to SF1000.
- **Absorbing a change incrementally beat rebuilding, more so at scale.** Against DuckDB's rebuild on the same
  machine, Duckstring's change was 2.3 to 4.2 times faster at SF100 and 4.5 to 12.9 times at SF1000; on the
  laptop, 3.4 to 13 times. It also needed far less memory: 14 to 24 GB at SF1000 against the rebuild's 110.
- **The cost of a change follows what it touches.** Ten times the data made the append change 3.2 times slower
  and the mixed change 4.9 times: finding the sales of changed dimension rows means reading the fact's key column.
  At SF1 and SF10 the same 5,000 new sales read about 35,000 rows from the fact at both scales (audit.py).
- **Duckstring's first build costs about what a rebuild does, on the machine.** It's 19 to 20 s against DuckDB's
  17 s at SF100, and 162 to 163 s against 166 s at SF1000 (the append case is slower, 24 s and 210 s, from landing
  the history as an append table). Copying its storage to S3 adds about 16 s at SF100 and three minutes at SF1000.
- **The laptop is a fair proxy for SF100.** DuckDB took 25 s on the laptop and 17 s on 32 EC2 cores; Duckstring's
  changes were faster on the laptop (2.0 to 7.4 s) than on EC2 (4.0 to 7.7 s), because a change is many small
  steps and on EC2 each one makes round trips to S3.

## Caveats and what was tried

- **StarRocks' input layout.** On a single 4.3 GB Parquet file StarRocks' join was slower: split into 256 MB
  files, the SF100 mixed rebuild took 50.7 s instead of about 60. The SF1000 comparison uses split files for
  StarRocks and Spark, and DuckDB's time is the same on either layout.
- **StarRocks' schema inference** was ruled out: declaring every FILES() read's schema (4.1.2+) gave 60.8 and
  66.9 s against 64.3 s inferred.
- **StarRocks' aggregates re-read the enriched table from S3** (67 GB at SF1000, against DuckDB's 47.5 GB);
  DuckDB keeps recently read S3 data in memory within a run. StarRocks' data cache is off by default for FILES()
  and was left off, as no engine carried a cache between runs. At SF100 StarRocks' join alone (22 to 34 s) took
  longer than DuckDB's whole rebuild, so a cache wouldn't change the ordering there.
- **The dimensions Pond** fingerprints each newly landed extract (a row count and a sum of row hashes) and merges
  it only when that differs from the last one merged. In every scenario fresh item and customer extracts land,
  unchanged in the append and fact cases, and checking them takes 1 to 2 s of each change on EC2 (the join waits
  for it).
- **The 32-vCPU quota** in us-east-1 limits every cluster to the single machine's total. Scaling beyond one machine
  isn't measured.
- **Spark at 1 TB on one machine** wasn't run: Spark at that size runs on a cluster.
- **Spark's shuffle partitions.** Spark ran at its default of 200 shuffle partitions, like every engine at its
  defaults. That default suits SF100 and is known to be too few at SF1000, where each partition holds several GB
  and spills; a Spark user at that scale would usually raise it to a few thousand. The join grew about 18 times from
  SF100 to SF1000 for ten times the data, so part of Spark's SF1000 time is this setting. Not measured, but
  projected: with partitions sized to the data a task at SF1000 does about what one did at SF100, so the join
  would scale about tenfold (77 s to about 770 s) and the aggregates stay at their measured 180 s, for about
  950 to 1,250 s in all, still 6 to 7.5 times DuckDB's 166 s. The results here are Spark as it ships.

## Other runs in results/aws

- **20261026T223009Z-sf100_continuous** (one m7gd.8xlarge): the mixed set with eight change batches, the last seven
  released ten seconds apart under a continuous Refresh, with memory slots on and off. Each batch reached all three
  Outlets in 7.5 to 10.2 s either way, and both runs' outputs matched DuckDB's rebuild of the final snapshot.
- **20261026T224139Z to 224153Z-sf100_stress_{off,n4,n2,n1}** (four c7gd.2xlarge, 8 vCPU and 16 GB): the same
  continuous set, one memory-slot setting per machine. Two slots absorbed the backlog fastest (87 s from the first
  release, against 110 s with slots off, 119 s with four and 100 s with one). All matched DuckDB.

Every Duckstring run here also records each Ripple Run's start and end with its freshness (`runs_*.json`), the
Catchment's status every 0.25 s (`status_*.jsonl`) and each Duck's memory (`mem_*/`); `report_run.py` draws one
run's timeline from them.
