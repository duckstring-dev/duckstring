---
title: "Rank-Based Clustering for Improved Pipeline Performance"
slug: clustering-for-pipelines
date: 2026-10-06
authors: [isaac]
tags: [duckdb, parquet, performance]
description: How to order Parquet files for efficient pipelines using multiple columns for joins.
draft: true
---

The DuckDB team wrote a great post on why you might worry about the write order in tables prepared for analytical purposes:
[Faster Dashboards with Multi-Column Approximate Sorting](https://duckdb.org/2025/06/06/advanced-sorting-for-fast-selective-queries). 
It's a great introduction to space-filling curves and why they're 
relevant for optimizing queries that could target multiple columns - well worth a read. The gist is that 
the multiple columns used for some downstream purpose (e.g. querying or joins) form a multi-dimensional space, but 
the row order - imperative for skipping row groups - is inherently one-dimensional. The task for optimizing for 
multi-column queries becomes one of effectively reducing this multi-dimensional space to a one dimensional order.

The key insight of this article is that space-filling the *potential* values in the dataset is necessarily sub-optimal. 
By first *ranking* the values from each column, and interleaving those *ranks*, much better performance can be achieved.
This is valuable when writes are irregular, such as during a compaction operation over an appended changelog, so that the
ranks remain relevant. Here I present a variety of alternative methods for comparison on the TPC-DS dataset.

<!-- truncate -->


## Why order matters for query performance

In a Parquet file, rows are grouped into chunks called row groups (DuckDB writes 122,880 rows
per group by default), and within each group every column is stored and compressed separately. Alongside the
data, the file's footer records statistics for each column in each row group, including its smallest and
largest value. If you ask for `WHERE price BETWEEN 400 AND 430`, DuckDB reads the
footer first, and any row group whose `price` range is, say, 2 to 380 can't possibly contain a match. DuckDB
skips it without decompressing a byte. These per-group ranges are often called zone maps.

If rows are ordered randomly (the worst case), every row group contains a similar distribution of values. Every 
footer's min/max values are nearly identical, spanning the full set of values, so no query can avoid reading
everything.

If only one column is required downstream, then ordering by that column does a fantastic job (theoretical best) at splitting the
row groups - for a unique column, any target value could only exist in exactly one row group. For multiple columns though,
there is a trade off - ordering first by the first 'most important' column might mean the second column is barely
ordered at all (depending on the first's cardinality). Typical lexicographic ordering like this can help very little if
the columns are not selected carefully.

![Four small tables, each 32 by 32 cells, showing the min/max box of every row group under random order, a sort, Z-order and Hilbert order, with a horizontal band for a filter on the second column](/img/blog/clustering/row-groups.svg)

The figure above shows the problem on a toy table: every cell of a 32 by 32 grid, written in four different orders
and split into row groups of 48 rows. Each box is one row group's min/max range, and the shaded band is a filter on
the second column. In random order every box spans the whole grid, so all 22 row groups are read. Sorting by the
first column and then the second gives tall, thin stripes - ideal for a filter on the first column, but a filter on
the second still reads 21 of the 22.

## Space-filling curves for dimension reduction

Space-filling curves are a mathematical construction that threads a one-dimensional path (curve) through a higher-dimensional
space. They are self-similar at various scales, and as their fidelity increases, it enables these higher-dimensional spaces 
to be mapped entirely by the path.
This is very useful in multiple fields, due to these mappings approximately preserving proximity in the higher-dimensional
space when condensed down to the single dimension path.

The Hilbert curve is a particularly beautiful variant, where each step is adjacent to the previous - a property that 
also makes it near theoretical best for the purpose of dimension reduction. The Morton 'Z-order' however is used 
more commonly, as its construction is very straightforward:  the first bit of the first column is taken, then the first of the 
second, and so on, then the second bit of the first column, until some bit depth is reached or the data is exhausted. 
This has the neat effect of drawing a series of 'Z' shapes, themselves ordered into 'Z' shapes, snaking their way through 
the multi-dimensional space.

![Z-order and Hilbert curves drawn over an 8 by 8 grid](/img/blog/clustering/curves.svg)

Both visit every cell once, but the z-order makes long jumps at the edge of each quadrant, and a row group that
straddles one of those jumps ends up with a long, thin box. The Hilbert curve only ever steps to a neighbouring
cell, at the cost of a little more arithmetic. With row groups that don't line up neatly with the curve's quadrants
(and real row groups won't), this shows up directly: in the toy table the Hilbert order reads 5 row groups where the
z-order reads 8. The DuckDB post reaches the same conclusion, finding Hilbert the most consistent across its query
patterns.

## Why rank the values first

To place a row on the grid, each value first has to become an integer coordinate. The usual approach is to scale
it: take the column's min and max, split that range into equal-width buckets, and see which bucket each value falls
in. (The DuckDB post maps strings to integers from their first few characters, which amounts to the same thing.)
This fills the space of *potential* values, and it works well when values are spread evenly across their range.
Real columns rarely are. Here's `ss_net_paid` from TPC-DS, the amount paid per sale line, split into 16 buckets
both ways:

![Bar charts of rows per bucket for ss_net_paid: scaled buckets put 58% of rows in the first bucket; ranked buckets hold 6.25% each](/img/blog/clustering/buckets.svg)

Scaled between its min and max, 58% of rows land in the first bucket and the top half of the range is nearly
empty. The curve can only order rows by their buckets, so for most of the table this column contributes almost
nothing to the order.

Ranking fixes this. Each value is replaced by its quantile rank, with the bucket boundaries chosen so that every
bucket holds the same number of rows. Whatever the shape of the distribution, every column becomes uniform and gets
an equal share of the order. I'll call the result a 'rank-Hilbert' order, and 'rank-Morton' for the z-order
equivalent.

This isn't an entirely new idea. Delta Lake's `OPTIMIZE ... ZORDER BY` passes each column through a function called
`range_partition_id`, which its source describes as "an approximate rank() function", before interleaving the bits,
and its Liquid Clustering uses the same ranks with a Hilbert curve. What I wanted to know was how much each half of
that recipe matters, what it costs to compute, and how to do it well in plain DuckDB SQL.

## Setting up the comparison

I generated TPC-DS at scale factor 100 with DuckDB's `tpcds` extension and took the `store_sales` fact table: 288
million rows, 14.4 GB of Parquet. I then wrote it in each of these orders, with 122,880-row row groups:

| Order | What it is |
|---|---|
| Random | sorted by a hash of the primary key |
| Sorted | a plain `ORDER BY` over the clustering columns |
| Morton, Hilbert | the curve over each column scaled between its min and max |
| Rank-Morton, rank-Hilbert | the curve over each column's quantile rank |

I clustered on two sets of columns. The first pairs the sale date with the skewed `ss_net_paid`. The second is three
surrogate keys (date, item and customer), which are spread fairly evenly. Each layout then ran the same seeded set
of reads: ranges on each column selecting 0.1%, 1% and 10% of rows, boxes over all of the clustering columns, and a
few joins in the style of TPC-DS, where a filtered dimension table is joined to the fact table much as a pipeline
step would read it.

For each read I report two things. The share of rows read is the fraction of the table sitting in row groups whose
min/max can't rule the filter out, computed from the Parquet footers, so it doesn't depend on the machine. Query time
is the median of five warm runs on a 10-core laptop with 32 GB of memory, using DuckDB 1.5. Everything is in the
[benchmark directory](https://github.com/duckstring-dev/duckstring/tree/main/bench/clustering) for anyone who wants
to run it themselves.

## How the orders compare

### Ranking makes skewed columns useful

![Share of rows read for 1% ranges on ss_net_paid and on the sale date, for six orders](/img/blog/clustering/pruning-skewed.svg)

A range selecting 1% of `ss_net_paid` reads 82% of the table when it's sorted by date and then amount, since the
date comes first and the amount is barely ordered. The scaled curves do much better, at 16% for Morton and 12% for
Hilbert. Ranking first brings that down to 4.9% for rank-Morton and 3.5% for rank-Hilbert.

The ranked orders do give up a little on the date. A 1% date range reads 4.1% of rows under rank-Hilbert against
2.3% under plain Hilbert, because ranking spends some of the order on the amount that scaling had effectively left to
the date. That's the trade I'd want: scaling quietly hands one column almost everything, where ranking shares the
order out evenly.

Fewer rows read translates fairly directly into faster reads. With a warm cache, the 1% `ss_net_paid` range goes
from about 200 ms in random order to 19 ms in rank-Hilbert order:

![Query time for a 1% range on ss_net_paid under six orders, from 199 ms for random down to 19 ms for rank-Hilbert](/img/blog/clustering/query-time.svg)

These are warm-cache timings on a local SSD, so they measure the decoding and filtering that DuckDB avoids. When
reading from object storage, where every skipped row group is a request that never has to be made, I'd expect the
gap to be wider.

### Hilbert reads less than the z-order

![Share of rows read for 1% ranges on date, item and customer, for six orders clustered on all three](/img/blog/clustering/pruning-keys.svg)

On the three evenly spread keys, scaling and ranking come out almost the same, which is what you'd expect: the ranks
of a uniform column are just the column, rescaled. Here the curve is what separates the layouts. A 1% range on the
customer key reads 17% of rows under Morton and 10% under Hilbert, with or without ranking. Boxes selecting 0.1% of
rows across all three keys read under 1% of the table either way. The sorted layout shows the stripes from the toy
example at full scale: 1% of rows read for a date range, but 79% for item and 100% for customer.

One result surprised me. Even 'evenly spread' columns often aren't. TPC-DS sells about three times as much per day
from August to December as earlier in the year, so ranking gives November more buckets than May. A join selecting
November reads 9% of rows when ranked against 19% when scaled, while one selecting the second quarter reads 13%
ranked against 9% scaled. Ranking spends resolution where the rows are, which tends to be where the reads are too.

The joins on scattered dimension attributes (an item category, or customers born in a given month) read the whole
table under every order. DuckDB pushes the matching keys' min and max into the scan, and when those keys are spread
across the whole key range, no layout can help.

### What it costs to write

All of this is paid for once, when the table is written, and the sort itself is most of it:

![Time to write the clustered table for each order](/img/blog/clustering/write-cost.svg)

Writing the table without sorting took 31 seconds. The sorted layouts took between 160 and 215 seconds, and the
differences between them are mostly noise - the same layout varied by about 20% from run to run. Computing the
curve itself is a small part of this: the plain Morton key alone took 4 seconds and the rank-Hilbert key 18.

Ranking needs each column's bucket boundaries, which come from one aggregate pass per column, e.g.
`approx_quantile(col, [1/128, 2/128, ...])`. Approximate quantiles are sufficient for this purpose: they pruned just
as well as exact ones on every read I ran, and avoid sorting each column to find them.

### Sorting within files matters for DuckDB

Delta Lake doesn't sort rows within a file by default. It range-partitions rows into files by the curve key, so
each file covers a slice of the curve, and leaves the rows inside each file unordered. That makes sense for Delta,
which records each file's min and max in its transaction log and skips whole files without opening them - on object
storage, that saves a request per file. I tried the same, partitioning into files of 256 MB and 1 GB:

![Share of rows read when rows are sorted within files, against files that only cover key ranges, at 256 MB and 1 GB](/img/blog/clustering/file-level.svg)

It saved 25 to 40% of the write time, but read 4 to 8 times more rows with 256 MB files, and more again with 1 GB
files. DuckDB prunes at the row group, inside the file, so for DuckDB most of the benefit comes from the order
within each file. When DuckDB is the reader, it's worth sorting the whole thing.

## Trying it in plain DuckDB

None of this needs anything special. Here's the same approach in plain DuckDB SQL, ranking with `approx_quantile`
and walking the ranks with the `lindel` community extension's `hilbert_encode`, which the DuckDB post also uses:

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

The `list_filter` count is each row's bucket: the number of boundaries its value sits above. 64 buckets per column
makes 4,096 cells on the grid, which suits a table of a few thousand row groups. It's worth aiming for roughly one
cell per row group, since finer cells can't help a min/max check. On TPC-DS at scale factor 10 this clustered 29
million rows in about 6 seconds, and a 1% range on `ss_net_paid` then read 23 of 235 row groups. NULLs land in
bucket 0 here, which is fine for most tables.

## How Duckstring uses it

[Duckstring](https://duckstring.com) is a data engineering platform built on DuckDB, and it publishes every table as
Parquet. As of the next release, `cluster_by` uses the rank-Hilbert order described here. The columns to cluster on
are declared when a table is written:

```python
pond.merge_table("sales", rows, pk="ticket_id", cluster_by=["sold_date", "net_paid"])
```

Duckstring picks the number of buckets from the table's size, so that each cell is just under one row group. It
keeps a separate bucket for NULLs, uses exact quantiles for tables under ten million rows (so small tables come out
identical every time) and approximate ones above that, and only reorders a table's compacted history when it's being
rewritten anyway, so the sort is paid rarely. The
[guide](/guides/append_and_merge#clustering-a-merge-table) has the details.

## Summary

- When reads filter or join on a column, the order rows are written in decides how much of the table can be
  skipped, and matters more than almost anything else about the file.
- For a single column, a plain sort is hard to beat. For two or more, a Hilbert curve consistently read fewer rows
  than the z-order.
- Ranking each column before mapping it onto the curve gives skewed columns their share of the order, and costs
  little on evenly spread ones.
- Approximate quantiles are good enough for the ranks, and when DuckDB is the reader, sorting within files is well
  worth the extra write time.
- Every extra clustering column dilutes the others, so it pays to list only the columns that reads actually use.

The full benchmark, including every timing and the scripts to reproduce it, is in the Duckstring repository.
