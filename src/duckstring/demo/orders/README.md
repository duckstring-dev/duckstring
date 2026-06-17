# orders

Demo Duckstring Pond — an **append Trickle**. Each run appends a batch of new order lines to an
insert-only history table (`append_table`), each row stamped with the run's freshness. The single
history table is at once the full read and the delta source — downstream reads just the new lines.

Deploy to a Catchment:

```bash
duckstring pond deploy
```
