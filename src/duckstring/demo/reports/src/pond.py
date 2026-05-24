import time

from duckstring import ripple


@ripple
def monthly_summary(pond):
    time.sleep(1)
    lines = pond.read_table("sales.sale_line")
    summary = pond.con.sql("""
        SELECT
            YEAR(sale_date)                 AS year,
            MONTH(sale_date)                AS month,
            COALESCE(category, 'Unknown')   AS category,
            ROUND(SUM(revenue), 2)          AS total_revenue,
            SUM(quantity)                   AS units_sold,
            COUNT(*)                        AS tx_count
        FROM lines
        WHERE sale_date IS NOT NULL
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
    """)
    pond.write_table("monthly_summary", summary)
