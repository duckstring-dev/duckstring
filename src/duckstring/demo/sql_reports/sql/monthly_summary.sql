SELECT
    YEAR(sale_date)                 AS year,
    MONTH(sale_date)                AS month,
    COALESCE(category, 'Unknown')   AS category,
    ROUND(SUM(revenue), 2)          AS total_revenue,
    SUM(total_quantity)             AS units_sold,
    COUNT(*)                        AS tx_count
FROM sales.sale_line
WHERE sale_date IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3
