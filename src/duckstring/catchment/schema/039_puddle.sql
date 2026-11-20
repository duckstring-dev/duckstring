-- Puddle Sources on a dev Catchment (plans/showcase-fixes.md item 10). A deployed line may read some of its Sources
-- as Puddles: production's data, read through production's query API, at a position this Catchment moves through
-- production's history. Which Sources, and with which Puddle set, comes from the puddles.toml section named for
-- this Catchment, sent with each deploy or by `pond puddle push`: operational config outside the version, as
-- windows are. A deploy without one clears it.
CREATE TABLE pond_puddle_source (
    pond_id     INTEGER NOT NULL REFERENCES pond(id),
    source_name TEXT    NOT NULL,
    puddle_set  TEXT    NOT NULL DEFAULT 'default',
    PRIMARY KEY (pond_id, source_name)
);

-- The Puddle sets each version defines per Source ({source: [sets]}, JSON, from discovery), for checking a
-- section at deploy and at push. NULL: the definitions couldn't be loaded (puddles_error says why).
ALTER TABLE pond_version ADD COLUMN puddle_sets TEXT;
ALTER TABLE pond_version ADD COLUMN puddles_error TEXT;

-- A Puddle node: one per Source line read as a Puddle (named "{source}~puddle"), shared by every line reading it.
-- Drawn like a Draw: its freshness is its position, set by the Catchment, and it never sends demand upstream.
ALTER TABLE pond ADD COLUMN is_puddle INTEGER NOT NULL DEFAULT 0;
CREATE TABLE puddle (
    pond_id     INTEGER PRIMARY KEY REFERENCES pond(id),
    source_name TEXT    NOT NULL,
    origin      TEXT,              -- the registered Catchment name it was hydrated from (shown, never dialled)
    origin_url  TEXT,              -- the address it reads; the credentials are the secret PUDDLE_ORIGIN
    live        INTEGER NOT NULL DEFAULT 1,
    baseline_f  TEXT,              -- its position at the hydration point
    position_f  TEXT,              -- where it is now
    hydrated_at TEXT
);
