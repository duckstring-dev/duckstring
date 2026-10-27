-- The memory a Ripple holds outside DuckDB (@ripple(memory=), pond.toml [pond] memory), in bytes. The Catchment
-- reserves it while the Pond runs (plans/memory-budget.md). NULL: nothing declared.
ALTER TABLE ripple ADD COLUMN memory_bytes INTEGER;
