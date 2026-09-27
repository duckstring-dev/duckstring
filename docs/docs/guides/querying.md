---
title: Querying
description: Read published data from the CLI, Python, SQL clients and BI tools.
---

# Querying

Everything a Pond publishes can be queried through its Catchment, which acts as the catalog. This guide covers each way in, from a quick look on the command line to connecting a BI tool. Queries are read-only and never touch a running Pond's working database, so they can't interfere with a run.

## From the command line

Look at a table:

```bash
duckstring query reports monthly_summary
```

This prints the first 10 rows. Run any SQL against one Pond's tables with `--sql`, or keep it in a file:

```bash
duckstring query sales --sql "SELECT category, SUM(revenue) FROM sale_line GROUP BY 1 ORDER BY 2 DESC"
duckstring query sales --sql @queries/top_categories.sql
```

Save the result instead of printing it with `--csv`, `--json` or `--parquet` and a file name. Files go under `./ponds/{pond}/` unless you give `--path`.

## Across Ponds: the catalog

`duckstring query` works within one Pond. To join across Ponds, query the catalog, where each Pond is a schema:

```bash
duckstring serve query "
  SELECT r.year, r.month, r.category, r.total_revenue, s.units
  FROM reports_v1.monthly_summary r
  JOIN (SELECT category, SUM(total_quantity) AS units FROM sales_v1.sale_line GROUP BY 1) s USING (category)
"
```

`{pond}_v{major}` names a specific major version. The bare `{pond}` refers to the Pond's served major, which stays on the first major version deployed until you run `duckstring serve promote`. Name the version in anything long-lived, such as a dashboard, so a new major version never changes what it reads; see [Upgrades and Breaking Changes](upgrades.md#queries-by-name).

### What read-only users see

Users with a full-access key see every table. Users with a read-level key see only the tables each Pond exposes, and query in an isolated DuckDB that can't read files or network locations. A Pond lists its public tables in `pond.toml`:

```toml
[serve]
tables = ["monthly_summary"]
```

Operators can override this per table without redeploying:

```bash
duckstring serve expose reports monthly_summary --on
duckstring serve status        # what's exposed, per Pond
```

Keep intermediate tables unexposed, so consumers depend only on the tables a Pond intends to support.

## From Python

The [`Catchment`](../reference/python/catchment.md) client returns DuckDB relations, which convert to pandas, Arrow or Polars:

```python
from duckstring import Catchment

c = Catchment("http://catchment.internal:7474", api_key=READ_KEY)
summary = c.get("monthly_summary", pond="reports")
df = c.query("SELECT * FROM monthly_summary WHERE year = 2026", pond="reports").df()
print(c.tables(pond="reports"))
```

The client queries one Pond at a time, at its highest deployed major version.

## From a BI tool or SQL client

The Catchment can serve the catalog over the Postgres wire protocol, so psql, Metabase, Tableau and most Postgres drivers can connect. Enable it with an environment variable on the Catchment:

```bash
DUCKSTRING_SERVE_PG_PORT=5433 DUCKSTRING_SERVE_HOST=0.0.0.0 duckstring catchment start prod
```

Connect with any user name and database, and an API key as the password:

```bash
psql "host=catchment.internal port=5433 user=analyst dbname=duckstring password=$READ_KEY"
```

```sql
SELECT * FROM reports_v1.monthly_summary;
```

The key's level decides what's visible, as above. The password is sent in plain text, so only expose the port behind TLS or on a private network.

For columnar results in Python, the same catalog is available over Arrow Flight (`DUCKSTRING_SERVE_FLIGHT_PORT`). The ticket is the SQL text:

```python
import pyarrow.flight as flight

client = flight.FlightClient("grpc://catchment.internal:8815")
options = flight.FlightCallOptions(headers=[(b"authorization", f"Bearer {READ_KEY}".encode())])
table = client.do_get(flight.Ticket(b"SELECT * FROM reports_v1.monthly_summary"), options).read_all()
```

## From the web UI

Select a Pond in the UI to browse its tables, page through rows and run SQL against them. Objects, such as models and files, are listed there too, and can be downloaded with `duckstring get-object`.

## Refreshing on read

Queries return what has been published, however old. To refresh data when it's read, send a Tap before querying, from the application or with `duckstring trigger tap`. A Tap only runs upstream Ponds that have nothing newer to offer. See [Scheduling](scheduling.md#refresh-on-request).
