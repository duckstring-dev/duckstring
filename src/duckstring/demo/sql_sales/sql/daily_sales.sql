SELECT
    product_id,
    created_at          AS sale_date,
    SUM(quantity)       AS total_quantity,
    COUNT(*)            AS tx_count
FROM transactions.transaction
WHERE product_id IS NOT NULL
  AND quantity > 0
GROUP BY product_id, created_at
