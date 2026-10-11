# Trickle: incremental joins and aggregates for DuckDB

Trickle keeps tables in a DuckDB database up to date from the changes to their inputs. You write a join or an
aggregate once; each run recomputes only the rows affected by what changed since the last run.

```python
import duckdb
import trickle
from trickle import agg

con = duckdb.connect("shop.duckdb")

with trickle.run(con, pipeline="nightly") as t:
    # Full snapshots in, change sets out.
    t.merge_table("orders", con.sql("FROM read_parquet('drops/orders/*.parquet')"), pk="order_id")
    t.merge_table("products", con.sql("FROM read_csv('drops/products.csv')"), pk="product_id")

    # Written once; each run only recomputes what the changed orders and products affect.
    (t.source("orders")
       .join(t.source("products"), on="product_id")
       .select("s1.category, s0.quantity * s1.unit_price AS revenue")
       .aggregate(by="category", revenue=agg.sum("revenue"), orders=agg.count())
       .merge("revenue_by_category"))
```

Trickle is the incremental engine of [Duckstring](https://duckstring.com), a data engineering platform for
DuckDB, and is documented at [docs.duckstring.com](https://docs.duckstring.com). It is released under the
Apache License 2.0.
