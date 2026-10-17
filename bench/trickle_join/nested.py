"""A term `(O ⋈ P)' ⋈ δC`: does pushing δC's keys into O beat DuckDB's own plan? With P under an inner join and
under a left join. Run from the repo root: `uv run python bench/trickle_join/nested.py`."""

import statistics
import time

import duckdb

con = duckdb.connect()
con.execute("""
CREATE TABLE o AS SELECT i AS order_id, (hash(i) % 1000000)::INT AS product_id,
  (hash(i * 3) % 1000000)::INT AS cust_id, (hash(i * 7) % 8)::INT AS qty FROM range(15000000) t(i);
CREATE TABLE p AS SELECT i AS product_id, (i % 2000) * 0.5 AS price FROM range(900000) t(i);
CREATE TABLE c AS SELECT i AS cust_id, 'r' || (i % 7) AS region FROM range(1000000) t(i);
-- δC: 1 in 1,000 customers change region (a −1 of the old row, a +1 of the new)
CREATE TABLE dc AS SELECT cust_id, region || 'x' AS region, 1 AS d FROM c WHERE hash(cust_id) % 1000 = 0
          UNION ALL SELECT cust_id, region, -1 FROM c WHERE hash(cust_id) % 1000 = 0;
""")


def timed(sql, n=5):
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        row = con.execute(sql).fetchone()
        times.append(time.perf_counter() - t0)
    return statistics.median(times) * 1000, row


for how in ("JOIN", "LEFT JOIN"):
    o_pushed = "(SELECT * FROM o WHERE cust_id IN (SELECT cust_id FROM dc))"
    for name, o in (("no pushdown", "o"), ("keys pushed into o", o_pushed)):
        sub = f"(SELECT o.*, p.price FROM {o} o {how} p USING (product_id))"
        ms, row = timed(f"SELECT count(*), sum(d) FROM {sub} x JOIN dc USING (cust_id)")
        print(f"(o {how} p) ⋈ δC  {name:20s} {ms:6.0f} ms  {row}")

# A key on the NULL-padded side: (O ⟕ P) ⋈ δCat on P.cat_id, with 5 of 5,000 categories changed.
con.execute("""
CREATE TABLE p2 AS SELECT i AS product_id, (i % 5000)::INT AS cat_id FROM range(900000) t(i);
CREATE TABLE dcat AS SELECT i AS cat_id, 1 AS d FROM range(5) t(i);
""")
p_pushed = "(SELECT * FROM p2 WHERE cat_id IN (SELECT cat_id FROM dcat))"
for name, how, p in (("no pushdown", "LEFT JOIN", "p2"), ("pushed, left join kept", "LEFT JOIN", p_pushed),
                     ("pushed, tightened to inner", "JOIN", p_pushed)):
    ms, row = timed(f"SELECT count(*) FROM (SELECT * FROM o {how} {p} p USING (product_id)) x JOIN dcat USING (cat_id)")
    print(f"(o ⟕ p) ⋈ δCat  {name:28s} {ms:6.0f} ms  {row}")
