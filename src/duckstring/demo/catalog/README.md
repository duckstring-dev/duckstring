# catalog

Demo Duckstring Pond — a **merge Trickle**. Each run emits the complete product catalogue;
`merge_table(comprehensive=True)` diffs it against the prior state to derive the CDC (a drifted price →
an `upsert`, a discontinued product → a `delete`) into a `__changelog` companion, keeping a clean
current-state `product` main.

Deploy to a Catchment:

```bash
duckstring pond deploy
```
