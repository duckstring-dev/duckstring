---
title: "Get your rows in a row: clustering Parquet for DuckDB"
slug: clustering-parquet-for-duckdb
date: 2026-10-06
authors: [isaac]
tags: [duckdb, parquet, performance]
description: How the order you write rows in decides how much of a Parquet table DuckDB can skip, and why ranking each column before mapping it onto a Hilbert curve keeps every filter fast, skewed columns included.
draft: true
---

DuckDB is very fast at reading data. It's even faster at not reading it. When a query filters a big Parquet
table, most of the speed you see comes from the parts of the file DuckDB never had to decompress, and how much
it can skip depends almost entirely on one decision you may never have made on purpose: the order you wrote the
rows in.

This post is about choosing that order well when your queries filter on more than one column. On 288 million
rows of TPC-DS, the right order took a filtered query from about 200 ms to about 20 ms, where a plain sort only
halved it.

<!-- truncate -->

**TL;DR**

- Parquet files are split into row groups, each of which records the min and max of every column. DuckDB skips
  any row group whose range rules out your filter, so similar values stored together means less to read.
- Sorting by a column makes filters on that column fast. Sorting by several columns only really helps the first.
- A space-filling curve orders rows so they're clustered on several columns at once. The Hilbert curve does
  this better than the more common Z-order (Morton) curve.
- Curves are usually applied to raw values, which goes badly when a column is skewed. Rank each column first,
  so every bucket holds the same number of rows, and skewed columns cluster as well as even ones.
- Computing ranks with window functions doesn't scale (ours ran out of disk). Quantile boundaries from one
  `approx_quantile` pass, then a binary search per row, cost a few seconds on top of the sort.

## Row groups and the min/max trick

A Parquet file isn't one big blob. Rows are grouped into chunks called row groups (DuckDB writes 122,880 rows
per group by default), and within each group every column is stored and compressed separately. Alongside the
data, the file's footer records statistics for each column in each row group, including its smallest and
largest value.

That footer is what makes skipping possible. If you ask for `WHERE price BETWEEN 400 AND 430`, DuckDB reads the
footer first, and any row group whose `price` range is, say, 2 to 380 can't possibly contain a match. DuckDB
skips it without decompressing a byte. These per-group ranges are often called zone maps.

So the question becomes: how narrow are the ranges? If rows arrive in random order, every row group contains a
bit of everything, every range spans nearly the whole column, and DuckDB reads the lot. If similar values sit
together, each row group covers a thin slice, and a selective filter touches only a few of them.

Sorting is the obvious fix, and for one column it's the right one. Sort by `price` and each row group holds a
narrow band of prices. The trouble starts with the second column.

![Four small tables, each 32 by 32 cells, showing the min/max box of every row group under random order, a sort, Z-order and Hilbert order, with a horizontal band for a filter on the second column](/img/blog/clustering/row-groups.svg)

Sort by `(first, second)` and the row groups become tall, thin stripes: perfect for a filter on the first column,
useless for one on the second, because every stripe spans the second column's whole range. In this toy table a
filter on the second column still reads 21 of 22 row groups. Real dashboards and pipelines rarely filter on just
one thing.

## Space-filling curves

What we want is an ordering where rows that are close in the list are close in every column. Picture each row
as a point on a grid, one axis per column. A space-filling curve is a path that visits every cell of that grid
exactly once, and if we sort rows by their position along the path, nearby rows end up in the same row group.
Each row group then covers a compact box instead of a stripe, and a filter on any one column crosses only a
fraction of the boxes.

![Z-order and Hilbert curves drawn over an 8 by 8 grid](/img/blog/clustering/curves.svg)

The two classic curves:

- **Z-order** (also called the Morton order) interleaves the bits of each coordinate: the first bit of `x`,
  then the first bit of `y`, then the second bit of each, and so on. It's cheap to compute and widely used.
  Its weakness is the jumps you can see on the left: at the edge of each quadrant the path leaps across the
  grid, and a row group that straddles a leap gets a long, thin box.
- **Hilbert** visits the same cells but only ever steps to a neighbour. It takes more arithmetic to compute,
  and in return there are no leaps. In the toy table above, Hilbert order reads 5 row groups where Z-order
  reads 8.

The DuckDB team wrote a great post on exactly this,
[Faster Dashboards with Multi-Column Approximate Sorting](https://duckdb.org/2025/06/06/advanced-sorting-for-fast-selective-queries),
using the [`lindel`](https://duckdb.org/community_extensions/extensions/lindel) community extension's
`morton_encode` and `hilbert_encode` on US flight data. Their conclusion, that Hilbert gives the most consistent
performance across query patterns, matches what we found. Read it if you haven't; this post picks up where it
leaves off.

## The catch: curves cut values, not rows

To put a row on the grid, each value has to become an integer coordinate first. The natural way is to scale it:
take the column's min and max, divide the range into equal-width buckets, and see which bucket each value falls
in. (The DuckDB post maps strings to integers from their first few characters, which amounts to the same thing.)

That works beautifully for evenly spread columns. Real columns are rarely even. Here's TPC-DS's `ss_net_paid`,
the amount paid per sale line, split into 16 buckets both ways:

![Bar charts of rows per bucket for ss_net_paid: scaled buckets put 58% of rows in the first bucket; ranked buckets hold 6.25% each](/img/blog/clustering/buckets.svg)

Scaled between its min and max, 58% of all rows land in the first bucket and the top half of the range is
nearly empty. The curve can only order rows by their buckets, so for most of the table this column contributes
nothing to the ordering at all. A filter on a typical amount still reads a big slice of the table.

The fix is to cut by rows instead of by values: replace each value with its **quantile rank**, choosing bucket
boundaries so that every bucket holds the same number of rows. Skewed, lumpy or long-tailed, every column
becomes uniform, and every column gets an equal share of the ordering. We call the result a **rank-Hilbert**
order (rank each column, then walk the ranks along a Hilbert curve).

This isn't a new idea, and it's worth saying who got there first. Delta Lake's `OPTIMIZE ... ZORDER BY` passes
each column through a function called `range_partition_id`, which its own source describes as "an approximate
rank() function", before interleaving the bits. Delta's Liquid Clustering uses the same ranks with a Hilbert
curve. What we wanted to know was how much each half of that recipe matters, what it costs, and how to do it
well in plain DuckDB SQL.

## The experiment

We generated TPC-DS at scale factor 100 with DuckDB's `tpcds` extension and took the `store_sales` fact table:
288 million rows, 14.4 GB of Parquet. We wrote it in each of these orders, with 122,880-row row groups:

| Order | What it is |
|---|---|
| Random | sorted by a hash of the primary key |
| Sorted | a plain `ORDER BY` over the clustering columns |
| Morton, Hilbert | the curve over each column scaled between its min and max |
| Rank-Morton, rank-Hilbert | the curve over each column's quantile rank |

We clustered on two sets of columns. One pairs the sale date with the skewed `ss_net_paid`. The other is three
surrogate keys (date, item and customer), which are spread fairly evenly. Then we ran a seeded set of filters
against every layout: ranges on each column selecting 0.1%, 1% and 10% of rows, boxes over all the clustering
columns, and a few TPC-DS-style joins.

For each query we report two things. The share of rows read is the fraction of the table sitting in row groups
whose min/max can't rule the filter out, computed from the Parquet footers, so it doesn't depend on the machine.
Query time is the median of five warm runs on a 10-core laptop with 32 GB of memory, using DuckDB 1.5. Everything
is in the [benchmark directory](https://github.com/duckstring-dev/duckstring/tree/main/bench/clustering) if you
want to run it yourself.

## What we found

### Ranking rescues skewed columns

![Share of rows read for 1% ranges on ss_net_paid and on the sale date, for six orders](/img/blog/clustering/pruning-skewed.svg)

A range selecting 1% of `ss_net_paid` reads 82% of the table when it's sorted by date then amount (the date
comes first, so the amount is barely ordered), 16% under Morton and 12% under Hilbert. Ranking first brings that
down to 4.9% with Morton and 3.5% with Hilbert.

There's no free lunch: ranking spends some of the ordering on the amount that scaling had left to the date, so a
1% date range reads 4.1% of rows under rank-Hilbert against 2.3% under plain Hilbert. That's the trade you want.
Scaling quietly gives one column almost everything; ranking shares the ordering out evenly.

Fewer rows read means faster queries. With a warm cache, the 1% `ss_net_paid` range goes from about 200 ms in
random order to 19 ms in rank-Hilbert order:

![Query time for a 1% range on ss_net_paid under six orders, from 199 ms for random down to 19 ms for rank-Hilbert](/img/blog/clustering/query-time.svg)

These are warm-cache timings on a local SSD, so they measure the decoding and filtering DuckDB avoids. Reading
from object storage, where every skipped row group is a request you don't make, the gap would be wider.

### Hilbert beats Morton, consistently

![Share of rows read for 1% ranges on date, item and customer, for six orders clustered on all three](/img/blog/clustering/pruning-keys.svg)

On the three evenly spread keys, scaling and ranking come out almost the same, which is what you'd expect: ranks
of a uniform column are just the column, rescaled. The curve is what separates the layouts. A 1% range on the
customer key reads 17% of rows under Morton and 10% under Hilbert, with or without ranking. Boxes selecting 0.1%
of rows across all three keys read under 1% of the table either way.

The sorted layout shows the stripes problem at scale: 1% of rows read for a date range, and 79% or 100% for
item and customer.

One result surprised us. Even "evenly spread" columns often aren't. TPC-DS sells about three times as much per
day from August to December as earlier in the year, so ranking gives November more buckets than May. A join selecting November reads 9% of rows ranked
against 19% scaled; one selecting the second quarter reads 13% ranked against 9% scaled. Ranking spends
resolution where the rows are, which is usually where the queries are too.

### What it costs to write

All of this is paid for once, when the table is written. The sort itself is the bulk of it:

![Time to write the clustered table for each order, with the window-function rank-Morton marked as not finishing](/img/blog/clustering/write-cost.svg)

Writing the table without sorting took 31 seconds. The sorted layouts took between 160 and 215 seconds, apart
from one intermediate version we'll come to, and the differences between them are mostly noise (run to run, the
same layout varied by about 20%). Computing the curve is a small
part of it: building the plain Morton key alone took 4 seconds and the rank-Hilbert key 18.

That dashed bar is our own first attempt. Duckstring 0.6.0 computed ranks with `NTILE` window functions, the
textbook way to bucket rows by rank:

```sql
NTILE(128) OVER (ORDER BY ss_net_paid)
```

It's correct, and it falls over at scale. Each window sorts every full row of the table, once per column, before
the final sort even starts. At 288 million rows it filled 60 GB of spill space and gave up. It also has a subtle
flaw: rows with equal values can be split across a bucket boundary in whatever order they happen to arrive, so
the same data could produce different layouts.

The fix is to compute the bucket boundaries first and then look each row up:

1. One aggregate pass per column gets the boundaries: `approx_quantile(col, [1/128, 2/128, ...])`. It's a
   streaming sketch (a t-digest), so it never sorts anything, and approximate boundaries pruned just as well as
   exact ones on every query we ran.
2. Each row finds its bucket by binary search over the boundaries. Neatly, the binary search's comparisons are
   the rank's bits, most significant first: "above the median?" is the top bit, "above the quartile on that
   side?" the next, and so on. Seven comparisons give 128 buckets.

Because a boundary is a value, equal values always land in the same bucket, so the layout is deterministic too.
With exact quantiles but one comparison per boundary, our intermediate version took 304 seconds. Approximate
boundaries with the binary search took 203.

### Why not just sort between files?

Delta Lake, by default, doesn't sort rows within a file at all. It range-partitions rows into files by the curve
key, so each file covers a slice of the curve, and leaves each file's rows unordered. That makes sense for Delta:
it records each file's min and max in its transaction log and skips whole files without opening them, which on
object storage saves a request per file.

We tried it, partitioning into files of 256 MB and 1 GB:

![Share of rows read when rows are sorted within files, against files that only cover key ranges, at 256 MB and 1 GB](/img/blog/clustering/file-level.svg)

It saved 25 to 40% of the write time, and read 4 to 8 times more rows with 256 MB files, more with 1 GB ones.
DuckDB prunes at the row group, inside the file, so for DuckDB the order within each file is where most of the
benefit lives. If you write Parquet for DuckDB to read, sort the whole thing.

## Doing it yourself in DuckDB

You don't need anything special to try this. Here's the recipe in plain DuckDB SQL, ranking with
`approx_quantile` and walking the ranks with `lindel`'s `hilbert_encode`:

```sql
INSTALL lindel FROM community;
LOAD lindel;

COPY (
    WITH bounds AS (
        SELECT
            approx_quantile(ss_sold_date_sk, [i / 64 FOR i IN range(1, 64)]::FLOAT[]) AS d,
            approx_quantile(ss_net_paid,     [i / 64 FOR i IN range(1, 64)]::FLOAT[]) AS p
        FROM store_sales
    )
    SELECT s.*
    FROM store_sales s, bounds
    ORDER BY hilbert_encode([
        len(list_filter(d, b -> b < s.ss_sold_date_sk)),
        len(list_filter(p, b -> b < s.ss_net_paid))
    ]::UTINYINT[2])
) TO 'store_sales_clustered.parquet';
```

The `list_filter` count is each row's bucket: how many boundaries its value is above. 64 buckets per column
makes 4,096 cells on the grid, which suits a table of a few thousand row groups; aim for roughly one cell per row
group, since finer cells can't help a min/max check. On TPC-DS at scale factor 10 this clustered 29 million rows
in about 6 seconds, and a 1% range on `ss_net_paid` then read 23 of 235 row groups. (NULLs land in bucket 0
here, which is fine for most tables.)

## In Duckstring

[Duckstring](https://duckstring.com) is a data engineering platform built on DuckDB, and it publishes every table
as Parquet. As of the next release, `cluster_by` uses the rank-Hilbert order described here. Declare the columns
your readers filter on when you write a table:

```python
pond.merge_table("sales", rows, pk="ticket_id", cluster_by=["sold_date", "net_paid"])
```

Duckstring picks the number of buckets from the table's size, so each cell is just under one row group. It
keeps a separate bucket for NULLs, uses exact quantiles for tables under ten million rows (so small tables come
out identical every time) and approximate ones above, and reorders a table's compacted history only when it's
rewritten anyway, so the sort is paid rarely. The
[guide](/guides/append_and_merge#clustering-a-merge-table) has the details.

## Takeaways

- If your queries filter on a column, the order you write rows in matters more than almost anything else you can
  change about the file.
- For one column, sort. For two or more, use a Hilbert curve, and rank each column first so skewed columns get
  their share.
- Keep the column list short. Every extra column dilutes the others, so list the ones your filters actually use.
- Compute ranks from quantile boundaries, never with window functions, and sort within files when DuckDB is the
  reader.

The full benchmark, including every timing and the scripts to reproduce it, is in the Duckstring repository.
Happy clustering!
