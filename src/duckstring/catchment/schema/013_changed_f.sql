-- Content freshness: the freshness at which a Pond's OUTPUT last actually changed (<= end_f). A pass
-- (no-change run) advances end_f but holds this, so downstream can skip work. See plans/no-change-skip.md.
-- Backfill = end_f: treat each Pond's last completed run as a change, so an upgraded Catchment doesn't
-- do one redundant real run per Pond before settling.
ALTER TABLE pond_state ADD COLUMN changed_f TEXT;
UPDATE pond_state SET changed_f = end_f;

-- Per-run "did the output change" flag. A pass (engine-synthesised no-change run, or a Duck reporting
-- an empty delta / pond.skip()) records changed = 0; a real run that produced new output records 1.
ALTER TABLE pond_run ADD COLUMN changed INTEGER NOT NULL DEFAULT 1;

-- A Ripple declared @ripple(always_run=True): the Pond runs every time even when its Sources are
-- unchanged (a side effect must fire); ORed up to the Pond. See plans/no-change-skip.md.
ALTER TABLE ripple ADD COLUMN always_run INTEGER NOT NULL DEFAULT 0;
