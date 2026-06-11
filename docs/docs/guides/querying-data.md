---
title: Querying Data
description: Retrieve Pond outputs — files, SQL, and exports.
---

# Querying Data

Every successful Pond Run exports the Pond's tables as Parquet snapshots — its published output. The query surface reads **only these snapshots**, never a Pond's live working database, so a query always sees a consistent, completed result and never contends with a running transform.

## Quick look

The fastest glimpse of a table:

```bash
duckstring query reports monthly_summary
```

This runs `SELECT * FROM reports.monthly_summary LIMIT 10` and prints the result. As everywhere, `-c {name}` targets a non-default Catchment.

## SQL queries

Arbitrary SQL runs against the Pond's exported tables with `--sql`:

```bash
duckstring query reports --sql "SELECT category, SUM(total_revenue) FROM monthly_summary GROUP BY 1"
```

Tables can be addressed bare (`monthly_summary`) or qualified (`reports.monthly_summary`). The dialect is DuckDB's, executed by an ephemeral in-memory engine over the snapshots — full analytical SQL, zero impact on the pipeline.

Longer queries can live in files, referenced with `@`:

```bash
duckstring query reports --sql @queries/monthly_rollup.sql
```

## Writing results to files

Add a format flag with a filename to save instead of print:

```bash
duckstring query reports monthly_summary --csv summary.csv
duckstring query reports --sql @rollup.sql --json rollup.json
duckstring query reports monthly_summary --parquet summary.parquet
```

Files land in `./ponds/{pond}/{ripple}/` by default (or `./ponds/{pond}/` for pure-SQL queries); `--path {dir}` overrides the directory:

```bash
duckstring query reports --sql @rollup.sql --csv rollup.csv --path .
```

## Fetching raw output

To take a Ripple's entire published output as-is — no SQL involved:

```bash
duckstring get reports monthly_summary
duckstring get reports monthly_summary --path ./exports/summary
```

This downloads the Ripple's exported data (as Parquet) into `./ponds/reports/monthly_summary/` or the given `--path`. It works for any published output, including non-tabular content, which `query` can't address.

## Consuming from applications

Applications have the same two options, over HTTP (see the [HTTP API](../reference/http-api.md)):

- `POST /api/query` — JSON rows back from SQL, or CSV/JSON/Parquet with a `format` field.
- `GET /api/ponds/{pond}/ripples/{ripple}` — the raw exported file.

A pattern worth knowing from the [trigger model](triggers.md): pair each application read with a **Tap** on the Pond it reads. The pipeline then refreshes at the pace of actual consumption — each query effectively orders the next batch, and an unused Outlet costs nothing.

For heavier integration, the snapshots themselves are plain Parquet files under the Catchment root (`ponds/{pond}/data/{table}.parquet`) — directly readable by anything that speaks Parquet, no Duckstring involved.
