-- A Spout is a passive **standing-Wake** node hanging off its Pond (see plans/egress.md): it runs
-- whenever its source Pond's freshness advances past what it has delivered — and never solicits the source
-- (a Wake, not a Wave, so it adds no upstream demand) and never blocks anything (it is terminal). The
-- Control verbs apply: Sleep/Kill disarm the standing Wake; Wake/Force re-arm it.
ALTER TABLE pond_spout ADD COLUMN standing_wake INTEGER NOT NULL DEFAULT 1; -- armed: deliver on a source advance
ALTER TABLE pond_spout ADD COLUMN is_killed     INTEGER NOT NULL DEFAULT 0; -- operator Kill: parked until Wake/Force/Clear
