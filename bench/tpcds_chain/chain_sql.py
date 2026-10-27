"""The chain as plain SQL, shared by the DuckDB and Spark baselines.

The Duckstring Ponds in ponds/ compute the same four tables with the Trickle builder. Each query reads the
tables named in it as views the runner registers over the snapshot's Parquet files (and `enriched` over the
first stage's output), and is written in the subset of SQL both engines read the same way.
"""

ENRICHED = """
SELECT
  s.ss_ticket_number, s.ss_item_sk, s.ss_customer_sk,
  d.d_date, d.d_year, d.d_moy, d.d_qoy,
  i.i_category, i.i_class, i.i_brand,
  st.s_store_name, st.s_state,
  ca.ca_state,
  s.ss_quantity, s.ss_net_paid, s.ss_net_profit,
  coalesce(r.sr_return_amt, CAST(0 AS DECIMAL(7,2))) AS return_amt,
  s.ss_net_paid - coalesce(r.sr_return_amt, CAST(0 AS DECIMAL(7,2))) AS net_revenue
FROM store_sales s
JOIN date_dim d ON s.ss_sold_date_sk = d.d_date_sk
JOIN item i ON s.ss_item_sk = i.i_item_sk
JOIN store st ON s.ss_store_sk = st.s_store_sk
LEFT JOIN customer c ON s.ss_customer_sk = c.c_customer_sk
LEFT JOIN customer_address ca ON c.c_current_addr_sk = ca.ca_address_sk
LEFT JOIN store_returns r ON s.ss_ticket_number = r.sr_ticket_number AND s.ss_item_sk = r.sr_item_sk
"""

AGGREGATES = {
    "category_month": """
SELECT d_year, d_moy, i_category,
       sum(net_revenue) AS revenue, sum(ss_quantity) AS units, count(*) AS sales, sum(ss_net_profit) AS profit
FROM enriched
GROUP BY d_year, d_moy, i_category
""",
    "store_quarter": """
SELECT s_store_name, s_state, d_year, d_qoy,
       sum(net_revenue) AS revenue, sum(return_amt) AS returns, avg(ss_net_paid) AS avg_paid
FROM enriched
GROUP BY s_store_name, s_state, d_year, d_qoy
""",
    "customer_value": """
SELECT ss_customer_sk, ca_state,
       sum(net_revenue) AS revenue, count(*) AS purchases, max(d_date) AS last_purchase
FROM enriched
WHERE ss_customer_sk IS NOT NULL
GROUP BY ss_customer_sk, ca_state
""",
}

SOURCES = ("store_sales", "store_returns", "item", "store", "date_dim", "customer", "customer_address")
