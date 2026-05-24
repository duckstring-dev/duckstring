-- Abstract named entity. Kind is a design property, not a version property.
-- git_branch: if set, deployments pull from this branch and read the version from pond.toml.
CREATE TABLE pond (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('inlet', 'pond', 'outlet')),
    git_branch TEXT
);

-- A specific deployed version of a Pond. source_path holds the materialised snapshot.
-- At most one is_active per (pond_id, major), enforced by the partial index below.
-- is_stopped=1 by default: a newly deployed pond waits for an explicit trigger.
CREATE TABLE pond_version (
    id                INTEGER PRIMARY KEY,
    pond_id           INTEGER NOT NULL REFERENCES pond(id),
    version           TEXT    NOT NULL,  -- full semver e.g. "1.2.3"
    major             INTEGER NOT NULL,
    is_active         INTEGER NOT NULL DEFAULT 0,
    is_stopped        INTEGER NOT NULL DEFAULT 1,
    immediate_retries INTEGER NOT NULL DEFAULT 0,
    source_retries    INTEGER NOT NULL DEFAULT 0,
    source_path       TEXT    NOT NULL,  -- relative to catchment root, under ponds/{name}/{version}/
    deployed_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (pond_id, version)
);

-- Only one active version per major per pond.
CREATE UNIQUE INDEX idx_pond_version_active ON pond_version(pond_id, major) WHERE is_active = 1;

-- Inter-pond source declarations from pond.toml [sources].
-- Consumer side references a specific pond_version (the declaration is version-specific).
-- Source side references the abstract pond + major (a version-line, not a specific version).
CREATE TABLE pond_to_pond (
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    source_pond_id  INTEGER NOT NULL REFERENCES pond(id),
    source_major    INTEGER NOT NULL,
    min_version     TEXT    NOT NULL,
    required        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (pond_version_id, source_pond_id)
);

-- Ripples within a specific pond_version.
CREATE TABLE ripple (
    id              INTEGER PRIMARY KEY,
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    name            TEXT    NOT NULL,
    UNIQUE (pond_version_id, name)
);

-- Intra-pond parent/child edgelist. All edges are implicitly required.
CREATE TABLE ripple_to_ripple (
    sink_id   INTEGER NOT NULL REFERENCES ripple(id),
    source_id INTEGER NOT NULL REFERENCES ripple(id),
    PRIMARY KEY (sink_id, source_id)
);

-- Demand initiation configuration for Outlet Ponds.
-- References pond (abstract): trigger config persists across version upgrades.
CREATE TABLE pond_trigger (
    id         INTEGER PRIMARY KEY,
    pond_id    INTEGER NOT NULL REFERENCES pond(id),
    kind       TEXT    NOT NULL CHECK (kind IN ('pulse', 'wave', 'tide')),
    schedule   TEXT,              -- cron expression; only for 'tide'
    status     TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stopped')),
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Active demand records. Rows are deleted (not flagged) when demand is cleared.
-- sink_id: the downstream pond_version that created this demand (null if trigger-sourced).
-- At most one row per (pond_version_id, non-null sink_id); idx_demand_trigger covers null.
CREATE TABLE demand (
    id              INTEGER PRIMARY KEY,
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    sink_id         INTEGER          REFERENCES pond_version(id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (pond_version_id, sink_id)
);
-- UNIQUE above doesn't enforce uniqueness for NULL sink_id (SQLite treats NULLs as distinct).
-- This partial index enforces at most one trigger-sourced demand per pond_version.
CREATE UNIQUE INDEX idx_demand_trigger ON demand(pond_version_id) WHERE sink_id IS NULL;

-- Stop signals, orthogonal to demand. A pond acknowledges stop when every demand row has
-- a corresponding stop row from the same sink (matched via IS for NULL-safe equality).
-- Same NULL uniqueness pattern as demand.
CREATE TABLE stop (
    id              INTEGER PRIMARY KEY,
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    sink_id         INTEGER          REFERENCES pond_version(id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_stop_unique  ON stop(pond_version_id, sink_id);
CREATE UNIQUE INDEX idx_stop_trigger ON stop(pond_version_id) WHERE sink_id IS NULL;

-- Last generation of each source Pond major consumed by each sink Pond.
-- References pond (abstract) so watermarks survive version upgrades within a major.
-- source_major is included because a sink may concurrently consume multiple major
-- versions of the same source.
CREATE TABLE watermark (
    sink_pond_id   INTEGER NOT NULL REFERENCES pond(id),
    source_pond_id INTEGER NOT NULL REFERENCES pond(id),
    source_major   INTEGER NOT NULL,
    generation     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sink_pond_id, source_pond_id, source_major)
);

-- Tracks the source generation at the time of the last failure. Prevents a pond in
-- source_retries mode from re-triggering on the same source data that already caused
-- a failure. Cleared on every successful run.
CREATE TABLE retry_watermark (
    sink_pond_id   INTEGER NOT NULL REFERENCES pond(id),
    source_pond_id INTEGER NOT NULL REFERENCES pond(id),
    source_major   INTEGER NOT NULL,
    generation     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sink_pond_id, source_pond_id, source_major)
);
CREATE INDEX idx_retry_watermark_sink ON retry_watermark(sink_pond_id);

-- A Pond execution. generation is per (pond, major) and is continuous across
-- version upgrades within the same major.
CREATE TABLE pond_run (
    id              TEXT    PRIMARY KEY,  -- UUID
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    generation      INTEGER NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT
);

-- A Ripple execution within a pond_run.
CREATE TABLE ripple_run (
    id          TEXT    PRIMARY KEY,  -- UUID
    pond_run_id TEXT    NOT NULL REFERENCES pond_run(id),
    ripple_id   INTEGER NOT NULL REFERENCES ripple(id),
    status      TEXT    NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    started_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    log_path    TEXT  -- relative to catchment root
);

CREATE INDEX idx_pond_version_pond   ON pond_version(pond_id);
CREATE INDEX idx_ripple_version      ON ripple(pond_version_id);
CREATE INDEX idx_demand_pvid         ON demand(pond_version_id);
CREATE INDEX idx_watermark_sink      ON watermark(sink_pond_id);
CREATE INDEX idx_pond_run_version    ON pond_run(pond_version_id);
CREATE INDEX idx_ripple_run_pond_run ON ripple_run(pond_run_id);
