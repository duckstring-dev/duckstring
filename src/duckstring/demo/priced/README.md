# priced

Demo Duckstring Pond — the **`pond.trickle(...)` builder**. Incrementally enriches `orders.order_line`
with `catalog.product` (join on `product_id`), re-pricing only the order lines a delta touches — a new
order, or one whose product price changed. The output is a merge Trickle for `revenue` to consume.

Sources: `orders`, `catalog`

Run locally (the puddles seed Trickle-shaped Sources):

```bash
duckstring pond hydrate
duckstring pond run
```

Deploy to a Catchment:

```bash
duckstring pond deploy
```
