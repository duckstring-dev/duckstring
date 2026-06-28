-- Windows on a Spout — throttle its standing Wake to a cadence (see plans/egress.md). Like an Inlet's
-- batch windows: when the Spout delivers, the freshness it records as delivered is clamped to the **end of
-- the current window** (not the source's freshness), so it won't deliver again until the source Pond's
-- freshness passes that window end — i.e. at most once per window. A separate table from `pond_window`
-- (which the engine reads for Inlet availability) so the two never cross.
CREATE TABLE spout_window (
    pond_id          INTEGER NOT NULL REFERENCES pond(id) ON DELETE CASCADE,
    spout_name       TEXT NOT NULL,
    name             TEXT NOT NULL,
    start_anchor     TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    freq_unit        TEXT NOT NULL,
    freq_interval    INTEGER NOT NULL DEFAULT 1,
    valid_days       TEXT,
    until_time       TEXT,
    PRIMARY KEY (pond_id, spout_name, name)
);
