# sales

Demo Duckstring Pond — enriches raw transactions with product catalogue data. Three Ripples run in order: `daily_sales` aggregates POS events to daily per-product totals, `price_tiers` classifies each product into budget/standard/premium, then `join_lines` joins them on `product_id` to produce enriched sale lines with computed revenue.

Sources: `transactions`, `products`

Deploy to a Catchment:

```bash
duckstring deploy <catchment>
```
