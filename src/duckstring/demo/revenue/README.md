# revenue

Demo Duckstring Pond — a **comprehensive merge Trickle** aggregating `priced.priced_line` to per-product
revenue. Re-aggregates each run, but only the products whose totals moved reach the changelog.

Sources: `priced`

Deploy fourth, then: `duckstring trigger pulse revenue`

```bash
duckstring pond deploy
```
