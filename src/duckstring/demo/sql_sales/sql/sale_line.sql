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
FROM daily_sales AS s
LEFT JOIN price_tiers AS p ON s.product_id = p.product_id
