# sales

Demo Duckstring Pond written as SQL Ripples. Each Ripple is a SQL file declared in `pond.toml`: `daily_sales` aggregates point-of-sale events to daily per-product totals, `price_tiers` classifies each product using the bands in `data/tier_bands.csv` (a static table), and `sale_line` joins the two once both have run.

Sources: `transactions`, `products`

Deploy to a Catchment:

```bash
duckstring pond deploy
```
