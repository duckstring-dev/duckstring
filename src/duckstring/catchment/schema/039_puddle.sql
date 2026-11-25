-- Puddles on a dev Catchment (plans/showcase-fixes.md item 10). A Puddle is a Source line on this Catchment that
-- stands in for the one in production, deployed from a reading Pond's Puddle definitions (`pond puddle deploy`):
-- a `pond` row like a Draw's (its freshness is its position, set by the Catchment; it never sends demand anywhere)
-- with `is_puddle` set, and every line here reading the Source reads it.
ALTER TABLE pond ADD COLUMN is_puddle INTEGER NOT NULL DEFAULT 0;
CREATE TABLE puddle (
    pond_id     INTEGER PRIMARY KEY REFERENCES pond(id),
    owner       TEXT    NOT NULL,  -- the Pond whose definitions it was deployed from (shown)
    puddle_set  TEXT    NOT NULL DEFAULT 'default',
    live        INTEGER NOT NULL DEFAULT 0,  -- asked for live (--live)
    mode        TEXT    NOT NULL,  -- what it is: live (read through production's query API) | copied (data here)
    origin      TEXT,              -- the registered Catchment name it reads (shown, never dialled); NULL: none
    origin_url  TEXT,              -- its address; the credentials are a secret named for it (catchment/puddles.py)
    as_at       TEXT,              -- the --as-at it was deployed or hydrated with (NULL: when that ran)
    baseline_f  TEXT,              -- its Source's freshness at the hydration point
    position_f  TEXT,              -- where it is now
    history     INTEGER NOT NULL DEFAULT 0,  -- a copy holding production's changes after the baseline
    runs        TEXT,              -- JSON: the freshnesses production reached after the baseline (a copy's history)
    steps       INTEGER NOT NULL DEFAULT 0,  -- has @puddle.step definitions
    step        INTEGER NOT NULL DEFAULT 0,  -- steps run since the baseline
    queries     TEXT,              -- JSON {table: sql}: a live Puddle's definitions, compiled when it was built
    kinds       TEXT,              -- JSON {table: [mode, pk]} for those queries
    hydrated_at TEXT
);
