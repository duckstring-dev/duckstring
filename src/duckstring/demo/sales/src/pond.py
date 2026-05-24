import time

from duckstring import ripple


@ripple
def stage_transactions(pond):
    time.sleep(2)
    raw = pond.read_table("transactions.transaction")
    staged = pond.con.sql("""
        SELECT
            id,
            created_at   AS sale_date,
            product_id,
            quantity,
            store_id
        FROM raw
        WHERE product_id IS NOT NULL
          AND quantity > 0
    """)
    pond.write_table("staged_transaction", staged)


@ripple
def stage_products(pond):
    time.sleep(1)
    raw = pond.read_table("products.product")
    staged = pond.con.sql("""
        SELECT id AS product_id, name, category, unit_price
        FROM raw
    """)
    pond.write_table("staged_product", staged)


@ripple(parents=[stage_transactions, stage_products])
def join_lines(pond):
    time.sleep(3)
    txns = pond.read_table("staged_transaction")
    prods = pond.read_table("staged_product")
    lines = pond.con.sql("""
        SELECT
            t.id                                    AS transaction_id,
            t.product_id,
            p.name                                  AS product_name,
            p.category,
            t.quantity,
            p.unit_price,
            ROUND(t.quantity * p.unit_price, 2)     AS revenue,
            t.sale_date,
            t.store_id
        FROM txns t
        LEFT JOIN prods p ON t.product_id = p.product_id
    """)
    pond.write_table("sale_line", lines)
