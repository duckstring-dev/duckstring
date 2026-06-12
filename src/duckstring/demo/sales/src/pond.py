import os
import time

from duckstring import ripple


def _mul() -> float:
    return float(os.environ.get("DUCKSTRING_SLEEP_MULTIPLIER", "1.0"))


@ripple
def daily_sales(pond):
    time.sleep(2 * _mul())
    pond.read_table("transactions.transaction")  # registers the view `transaction`
    agg = pond.con.sql("""
        SELECT
            product_id,
            created_at          AS sale_date,
            SUM(quantity)       AS total_quantity,
            COUNT(*)            AS tx_count
        FROM "transaction"
        WHERE product_id IS NOT NULL
          AND quantity > 0
        GROUP BY product_id, created_at
    """)
    pond.write_table("daily_sales", agg)


@ripple
def price_tiers(pond):
    time.sleep(1 * _mul())
    pond.read_table("products.product")  # registers the view `product`
    tiered = pond.con.sql("""
        SELECT
            id              AS product_id,
            name,
            category,
            unit_price,
            CASE
                WHEN unit_price < 25    THEN 'budget'
                WHEN unit_price < 150   THEN 'standard'
                ELSE                         'premium'
            END             AS price_tier
        FROM product
    """)
    pond.write_table("price_tiers", tiered)


@ripple(parents=[daily_sales, price_tiers])
def join_lines(pond):
    time.sleep(3 * _mul())
    # daily_sales and price_tiers are this Pond's own tables — query them directly.
    lines = pond.con.sql("""
        SELECT
            s.sale_date,
            s.product_id,
            p.name                                      AS product_name,
            p.category,
            p.price_tier,
            s.total_quantity,
            p.unit_price,
            ROUND(s.total_quantity * p.unit_price, 2)   AS revenue,
            s.tx_count
        FROM daily_sales s
        LEFT JOIN price_tiers p ON s.product_id = p.product_id
    """)
    pond.write_table("sale_line", lines)
