SELECT
    p.id            AS product_id,
    p.name,
    p.category,
    p.unit_price,
    b.price_tier
FROM products.product AS p
JOIN tier_bands AS b
  ON p.unit_price >= b.min_price AND p.unit_price < b.max_price
