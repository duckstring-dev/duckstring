-- One trigger, Refresh (plans/refresh-triggers.md). Tap, Wave, Pulse and Tide give way to Refresh, pull leaves
-- the engine, push is renamed demand, and the old control Refresh (a cold wipe-and-rebuild) becomes Rebuild.
--
-- Table rebuilds rather than DROP/RENAME COLUMN, so the migration runs on SQLite older than 3.35.

-- Standing Refreshes: any number per Pond. Floating (start_anchor NULL): due every_ms after the later of the
-- newest request and the freshness (0 = continuous). Scheduled: fires on the grid start_anchor + k * every_ms,
-- filtered by valid_days (CSV of MON..SUN, NULL = every day) and ended by until_time. UTC ISO-8601 times.
CREATE TABLE pond_refresh (
    pond_id      INTEGER NOT NULL REFERENCES pond(id),
    name         TEXT    NOT NULL,
    every_ms     INTEGER NOT NULL CHECK (every_ms >= 0),
    start_anchor TEXT,
    valid_days   TEXT,
    until_time   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (pond_id, name)
);

-- A Wave becomes a continuous Refresh; a Tide a floating one at its bound. Names follow the defaults the
-- Driver gives (default_refresh_name).
INSERT INTO pond_refresh (pond_id, name, every_ms)
SELECT pond_id,
       CASE
           WHEN kind = 'wave' OR COALESCE(bound_ms, 0) = 0 THEN 'Continuous'
           WHEN bound_ms = 604800000 THEN 'Weekly'
           WHEN bound_ms % 604800000 = 0 THEN 'Every ' || (bound_ms / 604800000) || ' weeks'
           WHEN bound_ms = 86400000 THEN 'Daily'
           WHEN bound_ms % 86400000 = 0 THEN 'Every ' || (bound_ms / 86400000) || ' days'
           WHEN bound_ms = 3600000 THEN 'Hourly'
           WHEN bound_ms % 3600000 = 0 THEN 'Every ' || (bound_ms / 3600000) || ' h'
           WHEN bound_ms % 60000 = 0 THEN 'Every ' || (bound_ms / 60000) || ' min'
           WHEN bound_ms % 1000 = 0 THEN 'Every ' || (bound_ms / 1000) || ' s'
           ELSE 'Every ' || printf('%g', bound_ms / 1000.0) || ' s'
       END,
       CASE WHEN kind = 'wave' THEN 0 ELSE COALESCE(bound_ms, 0) END
FROM pond_trigger
WHERE status = 'active';
DROP TABLE pond_trigger;

-- Push targets are a Pond's demand: the freshnesses it has been asked for and not yet met.
CREATE TABLE pond_demand (
    pond_id  INTEGER NOT NULL REFERENCES pond(id),
    demand_f TEXT    NOT NULL,
    PRIMARY KEY (pond_id, demand_f)
);
INSERT INTO pond_demand (pond_id, demand_f) SELECT pond_id, target_f FROM pond_target;
DROP TABLE pond_target;

-- pond_state without the pull columns (has_pull, has_received_pull, pull_local, pull_m), and refresh_pending
-- renamed rebuild_pending. A pending pull carries no demand forward: a Wave's Pond keeps its continuous
-- Refresh, which asks again on the next tick.
CREATE TABLE pond_state_new (
    pond_id         INTEGER PRIMARY KEY REFERENCES pond(id),
    start_f         TEXT,
    end_f           TEXT,
    d_ms            INTEGER NOT NULL DEFAULT 0,
    is_failed       INTEGER NOT NULL DEFAULT 0,  -- a Pond Run gave up, not yet superseded
    is_blocked      INTEGER NOT NULL DEFAULT 0,  -- this Pond, or a required Source, is failed/blocked/killed
    failed_f        TEXT,                        -- freshness of the freshest failed Run (NULL if none)
    failures        INTEGER NOT NULL DEFAULT 0,  -- failed Runs this episode (counted vs source_retries)
    is_killed       INTEGER NOT NULL DEFAULT 0,  -- operator Kill: terminal until Wake/Force/Clear
    rebuild_pending INTEGER NOT NULL DEFAULT 0,  -- the next Run is a cold wipe-and-rebuild
    repairing       INTEGER NOT NULL DEFAULT 0,
    changed_f       TEXT,
    persisted_f     TEXT
);
INSERT INTO pond_state_new (pond_id, start_f, end_f, d_ms, is_failed, is_blocked, failed_f, failures, is_killed,
                            rebuild_pending, repairing, changed_f, persisted_f)
SELECT pond_id, start_f, end_f, d_ms, is_failed, is_blocked, failed_f, failures, is_killed,
       refresh_pending, repairing, changed_f, persisted_f
FROM pond_state;
DROP TABLE pond_state;
ALTER TABLE pond_state_new RENAME TO pond_state;

-- Tap-on-get (a read on the query route fired a Tap) goes with pull; Taps (plans/taps.md) will replace it. An open
-- Pond keeps its open mark.
CREATE TABLE pond_open_new (
    pond_id INTEGER PRIMARY KEY REFERENCES pond(id)
);
INSERT INTO pond_open_new (pond_id) SELECT pond_id FROM pond_open;
DROP TABLE pond_open;
ALTER TABLE pond_open_new RENAME TO pond_open;
