# sales

Demo Duckstring Pond — enriches raw transactions with product catalogue data. Three Ripples run in order: `stage_transactions` and `stage_products` clean each source independently, then `join_lines` joins them into enriched sale line items.

Sources: `transactions`, `products`

Deploy to a Catchment:

```bash
duckstring deploy <catchment>
```
